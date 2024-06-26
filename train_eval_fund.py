import math
from model import ModelProgression
from torch import nn
import torch
import numpy as np
from functools import cached_property
from trainer import Trainer
from torch.utils.data import Dataset
import pandas as pd
import cv2
import albumentations as aug
import albumentations.pytorch as aug_torch


class DeepSurModel(nn.Module):
    def __init__(self, K=512) -> None:
        super().__init__()
        self.K = K
        # sample parameters for the mixture model
        rnd = np.random.RandomState(12345)
        b = torch.FloatTensor(abs(rnd.normal(0, 10, (1, 1, self.K))+5.0))##torch.Size([1, 1, self.K=512])
        k = torch.FloatTensor(abs(rnd.normal(0, 10, (1, 1, self.K))+5.0))##torch.Size([1, 1, self.K=512])
        self.register_buffer('b', b)
        self.register_buffer('k', k)

        self.cnn = ModelProgression(backbone='resnet50', output_size=512)

    def _cdf_at(self, t):
        # cdf: nBatch * n * K
        cdf = 1 - torch.exp(-(1/self.b * (t)) ** self.k)
        return cdf

    def _pdf_at(self, t):
        # pdf: nBatch * n * K
        pdf = self._cdf_at(t)
        pdf = (1-pdf) * self.k * (1/self.b)*(t/self.b)**(self.k-1)
        return pdf

    def calculate_cdf(self, w, t):
        """
        Calculates the cumulative probability distribution function (CDF)
        for the given data.

        param w: nBatch * K: weights for mixture model
        param t: nBatch * n: target time to calculate pdf at
        return: nBatch * n: cdf values
        """
        t = t.unsqueeze(dim=2)## t: nBatch * n * 1
        print('t.shape', t.shape)##t.shape torch.Size([2, 2, 1])
        w = nn.functional.softmax(w, dim=1)### ensure sum of w is 1, to mix the weibull distribution
        w = w.unsqueeze(dim=1)#3 w: nBatch * 1 * K
        print('w.shape', w.shape)##w.shape torch.Size([2, 1, 512])
        cdf = self._cdf_at(t)#3 pdf: nBatch * n * K
        print('cdf.shape', cdf.shape)##cdf.shape torch.Size([2, 2, 512])
        cdf = cdf * w## cdf: nBatch * n * K
        cdf = cdf.sum(dim=2)## cdf: nBatch * n
        print('cdf.shape', cdf.shape)##cdf.shape torch.Size([2, 2])
        return cdf

    def calculate_pdf(self, w, t):
        """
        Calculates the probability distribution function (pdf) for the given 
        data.

        param w: nBatch * K: weights for mixture model
        param t: nBatch * n: target time to calculate pdf at
        return: nBatch * n: pdf values
        """
        t = t.unsqueeze(dim=2)
        print('t.shape', t.shape)##t.shape torch.Size([2, 2, 1])##[1,199,1]
        w = nn.functional.softmax(w, dim=1)
        w = w.unsqueeze(dim=1)## w: nBatch * 1 * K
        print('w.shape', w.shape)##w.shape torch.Size([2, 1, 512])
        pdf = self._pdf_at(t)### pdf: nBatch * n * K
        print('pdf.shape', pdf.shape)##pdf.shape torch.Size([2, 2, 512])
        pdf = pdf * w## pdf: nBatch * n * K
        print('pdf.shape', pdf.shape)##pdf.shape torch.Size([2, 2, 512])
        pdf = pdf.sum(dim=2)
        return pdf## pdf: nBatch * n

    def calculate_survial_time(self, w, t_max=10, resolution=20):
        """
        Calculates the survival time for the given data.
        """
        t = torch.linspace(
            1/resolution,
            t_max,
            math.ceil(resolution*t_max)-1,
            dtype=torch.float32,
            device=w.device).view(1, -1)## t shape torch.Size([1, 199])
        
        # torch.linspace(start, end, steps, out=None, dtype=None, layout=torch.strided, device=None, requires_grad=False) → Tensor
        pdf = self.calculate_pdf(w, t)##nBatch * n
        
        est = t.view(-1)[torch.argmax(pdf, dim=1)]##在pdf 中取值最大的索引（表示最大概率得病的概率），然后对应到时间t上
        
        return est

    def forward(self, x, t=None):
        x = self.cnn(x)
        # print('x.shape', x.shape)##x.shape [batch=2, 512]
        if t is None:
            return x
        return x, self.calculate_cdf(x, t)


class ProgressionData(Dataset):

    def __init__(self, datasheet, transform):
        super().__init__()
        self.df = pd.read_csv(datasheet)
        self.transform = transform

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        img_file = self.df.iloc[idx]['image']
        # print('img_file--------',img_file)##data_fund/train_1.jpg
        # import os
        # img_file = os.path.join('./', img_file)
        # print('img_file--------',img_file)##data_fund/train_1.jpg
        image = cv2.imread(img_file, cv2.IMREAD_COLOR)
        
        image = self.transform(image=image)['image']
        return dict(
            image=image,
            t1=self.df.iloc[idx]['t1'],
            t2=self.df.iloc[idx]['t2'],
            e=self.df.iloc[idx]['e'],
            # simulation only
            gt=self.df.iloc[idx]['gt'] if 'gt' in self.df.columns else 0,
        )


class TrainerDR(Trainer):

    '''
    cached_property 是一个 Python 装饰器，用于将一个类的方法转换为属性，且该属性的值在第一次访问后会被缓存。这意味着，无论你多少次访问这个属性，方法只会在第一次访问时被调用，之后的访问都会直接返回缓存的结果。
这在你有一个计算成本较高的属性，且你知道在对象的生命周期中该属性的值不会改变的情况下非常有用。
    '''
    @cached_property
    def model(self):
        model = DeepSurModel().to(self.device)
        if self.cfg.load_pretrain is not None:
            print('loading ', self.cfg.load_pretrain)
            print(model.cnn.backbone.load_state_dict(
                torch.load(self.cfg.load_pretrain, map_location=self.device)
            ))
        return model

    @cached_property
    def beta(self):
        return 1

    @cached_property
    def train_dataset(self):
        transform = aug.Compose([
            aug.SmallestMaxSize(
                max_size=self.cfg.image_size, always_apply=True),
            aug.CenterCrop(self.cfg.image_size, self.cfg.image_size,
                           always_apply=True),
            aug.Flip(p=0.5),
            aug.ImageCompression(quality_lower=10, quality_upper=80, p=0.2),
            aug.MedianBlur(p=0.3),
            aug.RandomBrightnessContrast(p=0.5),
            aug.RandomGamma(p=0.2),
            aug.GaussNoise(p=0.2),
            aug.Rotate(border_mode=cv2.BORDER_CONSTANT,
                       value=0, p=0.7, limit=45),
            aug.ToFloat(always_apply=True),
            aug_torch.ToTensorV2(),
        ])
        return ProgressionData('data_fund/train.csv', transform)

    @cached_property
    def test_dataset(self):
        transform = aug.Compose([
            aug.SmallestMaxSize(
                max_size=self.cfg.image_size, always_apply=True),
            aug.CenterCrop(self.cfg.image_size, self.cfg.image_size,
                           always_apply=True),
            aug.ToFloat(always_apply=True),
            aug_torch.ToTensorV2(),
        ])
        return ProgressionData('data_fund/test.csv', transform)

    @cached_property
    def optimizer(self):
        optimizer = torch.optim.Adam(
            self.model.parameters(), lr=self.cfg.lr, weight_decay=1e-5)
        return optimizer

    def batch(self, epoch, i_batch, data) -> dict:
        # get and prepare data elements
        imgs = data['image'].to(self.device)
        t1 = data['t1'].to(self.device)
        t2 = data['t2'].to(self.device)
        print('t1', t1)##tensor([3, 1]
        print('t2', t2)##t2 tensor([5, 2],
        e = data['e'].to(self.device)

        w, P = self.model(imgs, torch.stack([t1, t2], dim=1))
        ## P:cdf: nBatch * n when training n=2
        print('w.shape', w.shape)##w.shape torch.Size([2, 512])
        w_sum = torch.sum(w, dim=1, keepdim=True)
        print('w_sum', w_sum)##w_sum tensor([[-10.1160],[ 11.1659]], device='cuda:0', grad_fn=<SumBackward1>)
        print('P.shape', P.shape)##
        P1 = P[:, 0]##cdf at t1 论文中的t_i
        P2 = P[:, 1]##cdf at t2 论文中的t_i'
        ###https://en.wikipedia.org/wiki/Weibull_distribution
        ##for weibull distribution the cdf is 1 - exp(-(t/b)^k), so 1-cdf = exp(-(t/b)^k)
        ## for weibull distribution the pdf is k/b * (t/b)^(k-1) * exp(-(t/b)^k)
        ### 
        ''' 
        for self.beta 
        @cached_property
        def beta(self):
            return 1
        so self.beta = 1
        '''
        loss = -torch.log(1-P1 + 0.000001) - torch.log(P2 +
                                                       0.000001) * self.beta * (e)
        loss += torch.abs(w).mean() * 0.00000001
        time_to_cal = torch.linspace(0, 20, 240).to(
            self.cfg.device).view(1, -1)
        cdf = self.model.calculate_cdf(w, time_to_cal)
        pdf = self.model.calculate_pdf(w, time_to_cal)
        survival_time = self.model.calculate_survial_time(w)
        print('survival_time', survival_time)##survival_time tensor([ 1.0000,  1.0000], device='cuda:0')
        return dict(
            loss=loss.mean(),
            pdf=pdf,
            cdf=cdf,
            t1=t1,
            t2=t2,
            survival_time=survival_time,
            gt=data['gt'],
        )

    def matrix(self, epoch, data) -> dict:
        return dict(
            loss=float(data['loss'].mean())
        )


if __name__ == '__main__':
    trainer = TrainerDR()
    trainer.train()
    # trainer.test()
