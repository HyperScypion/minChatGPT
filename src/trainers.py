import torch
from torch import nn
from torch.utils.data import DataLoader
from loss import KPairwiseLoss
import torch.optim as optim
from torch.cuda.amp.grad_scaler import GradScaler
import statistics
from tqdm import tqdm, trange
import time
from torch.utils.tensorboard import SummaryWriter
import os
import json
# import bitsandbytes as bnb

# torch.set_float32_matmul_precision('high')


class Trainer:

    def __init__(self) -> None:
        pass


class SFTTrainer(Trainer):

    def __init__(self, device, model: nn.Module, train_dataset, test_dataset,
                 total_epochs) -> None:
        super().__init__()
        pass

    def fit(self):
        pass


class RewardModelTrainer(Trainer):

    def __init__(self, device, model: nn.Module, train_dataset, test_dataset,
                 total_epochs, batch_size, name) -> None:
        super().__init__()
        self.run_name = int(time.time())
        self.device = device
        assert self.device == 'cuda'
        self.total_epochs = total_epochs
        self.eval_freq = 1
        self.save_freq = 20000
        self.model = model
        self.train_dataloader = DataLoader(train_dataset,
                                           batch_size=batch_size,
                                           num_workers=8,
                                           shuffle=True,
                                           pin_memory=True)
        self.test_dataloader = DataLoader(test_dataset,
                                          batch_size=batch_size,
                                          num_workers=8,
                                          pin_memory=True)
        self.model = model
        self.criterion = KPairwiseLoss()

        lr = 0.0001
        self.optimizer = optim.Adam(self.model.parameters(), lr=lr)
        self.grad_clip = 1.0
        self.dtype = torch.float16

        hp = {
            "grad_clip": self.grad_clip,
            "learning_rate": lr,
            "dtype": str(self.dtype),
            "batch_size": batch_size,
            "model": name,
            "lora_rank": model.cfg.lora_rank,
            "block_size": model.cfg.block_size,
        }
        if not os.path.exists(f'./runs/{self.run_name}'):
            os.makedirs(f'./runs/{self.run_name}')

        with open(f'./runs/{self.run_name}/hyperparams.json', 'w') as fp:
            json.dump(hp, fp, indent=4)

    def save_states(self, step, is_last=False):
        file_name = f'{self.run_name}_final.pt' if is_last else f'{self.run_name}_step{step}.pt'
        torch.save(
            {
                'step': step,
                'model_state_dict': self.model.state_dict(),
                'optimizer_state_dict': self.optimizer.state_dict(),
            }, f'./runs/{self.run_name}/{file_name}')

    def fit(self):
        self.model = torch.compile(self.model)
        self.model.to(self.device)
        writer = SummaryWriter(f'./runs/{self.run_name}/logs', max_queue=40)
        scaler = GradScaler(enabled=self.dtype != torch.float32)

        for epoch in range(self.total_epochs):
            losses = []
            self.model.train()
            for step, (completions, attention_masks) in enumerate(
                    pbar := tqdm(self.train_dataloader)):
                total_steps = step + epoch * len(self.train_dataloader)
                completions = completions.to(self.device)
                attention_masks = attention_masks.to(self.device)

                with torch.autocast(device_type=self.device, dtype=self.dtype):
                    # TODO: Support K completions instead of only 2
                    # TODO: Support gradient accumulation
                    positive_scores = self.model(
                        completions[:, 0, :],
                        attention_masks[:, 0, :])    # (B, 1)
                    negative_scores = self.model(
                        completions[:, 1, :],
                        attention_masks[:, 1, :])    # (B, 1)
                    loss = self.criterion(
                        torch.cat((positive_scores, negative_scores),
                                  dim=-1))    # (B, 2)

                if self.grad_clip != 0.0:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(),
                                                   self.grad_clip)

                scaler.scale(loss).backward()
                scaler.step(self.optimizer)
                scaler.update()
                self.optimizer.zero_grad(set_to_none=True)
                lossf = loss.item()
                writer.add_scalar('Loss/train/step', lossf, total_steps)
                losses.append(lossf)

                pbar.set_description(
                    f"batch loss {round(lossf,3)}, avg loss {round(statistics.mean(losses),3)}, "
                )

                if total_steps % self.save_freq == 0:
                    self.save_states(total_steps)

            epoch_loss = statistics.mean(losses)
            writer.add_scalar('Loss/train/epoch', epoch_loss, epoch)
            print(f'Epoch: {epoch+1}, Train Loss: {epoch_loss}')

            if epoch % self.eval_freq == 0:
                self.model.eval()
                with torch.no_grad():
                    tp = 0
                    total = 0
                    losses = []
                    for step, (completions, attention_masks) in enumerate(
                            self.test_dataloader):
                        completions = completions.to(self.device)
                        attention_masks = attention_masks.to(self.device)

                        positive_scores = self.model(
                            completions[:, 0, :],
                            attention_masks[:, 0, :])    # (B, 1)
                        negative_scores = self.model(
                            completions[:, 1, :],
                            attention_masks[:, 1, :])    # (B, 1)
                        loss = self.criterion(
                            torch.cat((positive_scores, negative_scores),
                                      dim=-1))    # (B, 2)
                        lossf = loss.item()
                        losses.append(lossf)
                        writer.add_scalar(
                            'Loss/test/step', lossf,
                            step + epoch * len(self.test_dataloader))
                        tp += torch.count_nonzero(
                            positive_scores > negative_scores)
                        total += positive_scores.shape[0]

                    acc = tp / total
                    epoch_loss = statistics.mean(losses)

                writer.add_scalar('Loss/test/epoch', epoch_loss, epoch)
                writer.add_scalar('Acc/test/epoch', acc, epoch)
                print(f'Epoch: {epoch+1}, Test Loss: {lossf}, Acc: {acc}')

        self.save_states(total_steps, True)
