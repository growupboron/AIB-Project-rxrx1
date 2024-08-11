import torch
import torchvision
from torch.utils.data import random_split
import torchvision.transforms as transforms
import torch.nn.functional as F
import torch.nn as nn
from wilds import get_dataset
from wilds.common.data_loaders import get_train_loader, get_eval_loader
from tqdm import tqdm
import os
from torch.utils.tensorboard import SummaryWriter
from dataset import RxRx1Dataset
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'
    dist.init_process_group("nccl", rank=rank, world_size=world_size)

def cleanup():
    dist.destroy_process_group()

def get_data_loaders():
    generator = torch.Generator().manual_seed(42)
    dataset = RxRx1Dataset(metadata_csv='metadata.csv', root_dir='/work/cvcs_2023_group23/AIB/data/rxrx1_v1.0', transform=transforms.Compose([transforms.CenterCrop(256), transforms.ToTensor(), transforms.Normalize(mean=(0.485, 0.456, 0.406), std = (0.229, 0.224, 0.225))]))
    total_size = len(dataset)
    train_size = int(0.8 * total_size)
    val_size = int(0.1 * total_size)
    test_size = total_size - train_size - val_size

    train_set, val_set, test_set = random_split(dataset, [train_size, val_size, test_size], generator=generator)

    train_loader = torch.utils.data.DataLoader(train_set, batch_size=256, shuffle=True, num_workers=4)
    eval_loader = torch.utils.data.DataLoader(test_set, batch_size=256, shuffle=True, num_workers=4)
    return train_loader, eval_loader

def nt_xent_loss(z, tau=0.5):
    N = z.size(0) // 2
    device = z.device
    cosine_sim = torch.nn.CosineSimilarity(dim=2)
    z_expanded = z.unsqueeze(1).repeat(1, 2*N, 1)
    z_tiled = z.repeat(2*N, 1).view(2*N, 2*N, -1)
    sim = cosine_sim(z_expanded, z_tiled) / tau
    mask = torch.eye(2*N, device=device).bool()
    sim.masked_fill_(mask, float('-inf'))
    sim_softmax = F.softmax(sim, dim=1)
    labels = torch.arange(2*N, device=device)
    labels = (labels + N) % (2*N)
    pos_probs = sim_softmax.gather(1, labels.view(-1, 1)).squeeze()
    loss = -torch.log(pos_probs + 1e-9).mean()
    return loss

class SimCLREncoder(nn.Module):
    def __init__(self, base_model, out_features):
        super(SimCLREncoder, self).__init__()
        self.base = nn.Sequential(*list(base_model.children())[:-1])
        self.projection_head = nn.Sequential(
            nn.Linear(base_model.fc.in_features, 512),
            nn.ReLU(),
            nn.Linear(512, out_features)
        )

    def forward(self, x):
        x = self.base(x)
        x = torch.flatten(x, 1)
        x = self.projection_head(x)
        return x

def get_simclr_augmentation_pipeline(input_size=256, type=1):
    return transforms.Compose([
        #transforms.ToPILImage(),
        transforms.RandomResizedCrop(size=input_size),
        #transforms.RandomHorizontalFlip(),
        #transforms.RandomGrayscale(p=0.2),
        transforms.ToTensor(),
    ]) if type == 1 else transforms.Compose([
        transforms.ToPILImage(),
        transforms.RandomRotation(degrees=180),
        #transforms.RandomVerticalFlip(),
        transforms.ToTensor(),
    ])

def save_checkpoint(state, epoch, base_dir="./checkpoints/parallel_noPL", filename="checkpoint_{epoch}.pth.tar"):
    os.makedirs(base_dir, exist_ok=True)
    filepath = os.path.join(base_dir, filename.format(epoch=epoch))
    torch.save(state, filepath)

def load_checkpoint(checkpoint_dir, model, optimizer):
    try:
        checkpoints = [chkpt for chkpt in os.listdir(checkpoint_dir) if chkpt.endswith('.pth.tar')]
        if not checkpoints:
            print("No checkpoints found at '{}', starting from scratch".format(checkpoint_dir))
            return 0
        latest_checkpoint = max(checkpoints, key=lambda x: int(x.split('_')[1].split('.')[0]))
        latest_checkpoint_path = os.path.join(checkpoint_dir, latest_checkpoint)

        print(f"Loading checkpoint '{latest_checkpoint_path}'")
        checkpoint = torch.load(latest_checkpoint_path)
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        epoch = checkpoint['epoch']
        print(f"Loaded checkpoint '{latest_checkpoint_path}' (epoch {epoch})")
        return epoch
    except Exception as e:
        print(f"Error loading checkpoint: {e}")
        return 0

def train(rank, world_size, epochs, start_epoch, train_loader, simclr_model, optimizer, augmentation_pipeline1, augmentation_pipeline2, temperature, checkpoint_dir):
    setup(rank, world_size)
    device = torch.device(f'cuda:{rank}')
    simclr_model.to(device)
    simclr_model = DDP(simclr_model, device_ids=[rank])

    writer = SummaryWriter(log_dir=f'./tb_logs/parallel_noPL_{rank}')

    simclr_model.train()
    for epoch in range(start_epoch, epochs):
        total_loss = 0
        with tqdm(total=len(train_loader), desc=f'Epoch {epoch+1}/{epochs}', unit='batch') as pbar:
            for batch_idx, (images, _, _) in enumerate(train_loader):
                optimizer.zero_grad()
                images_i = torch.stack([augmentation_pipeline1(img).to(device) for img in images])
                images_j = torch.stack([augmentation_pipeline2(img).to(device) for img in images])
                z_i = simclr_model(images_i)
                z_j = simclr_model(images_j)
                z = torch.cat((z_i, z_j), dim=0)
                loss = nt_xent_loss(z, temperature)
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
                writer.add_scalar('Loss/train', loss.item(), epoch * len(train_loader) + batch_idx)
                pbar.set_postfix({'Loss': total_loss / (batch_idx + 1)})
                pbar.update(1)
                if batch_idx % 100 == 0:
                    save_checkpoint({
                        'epoch': epoch + 1,
                        'state_dict': simclr_model.state_dict(),
                        'optimizer': optimizer.state_dict(),
                    }, epoch, base_dir=checkpoint_dir)
    writer.close()
    cleanup()

if __name__ == "__main__":
    checkpoint_dir = "./checkpoints/parallel_noPL"
    train_loader, eval_loader = get_data_loaders()
    base_model = torchvision.models.resnet18(weights=torchvision.models.ResNet18_Weights.DEFAULT)
    simclr_model = SimCLREncoder(base_model, out_features=4)
    optimizer = torch.optim.SGD(simclr_model.parameters(), lr=1e-3, momentum=0.8)
    start_epoch = load_checkpoint(checkpoint_dir, simclr_model, optimizer)
    temperature = 0.5
    world_size = torch.cuda.device_count()
    mp.spawn(train, args=(world_size, 50, start_epoch, train_loader, simclr_model, optimizer, get_simclr_augmentation_pipeline(), get_simclr_augmentation_pipeline(type=2), temperature, checkpoint_dir), nprocs=world_size, join=True)