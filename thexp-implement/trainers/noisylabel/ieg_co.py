"""
reimplement of 'Distilling Effective Supervision from Severe Label Noise'
other name of this paper(submission withdraw in ICLR2020) is 'IEG: Robust neural net training with severe label noises'
    https://arxiv.org/abs/1911.09781

0009.991b993d, 0.8, 92%, 似乎使用弱增广 计算 Loss 比强增广结果会好一些

0015.db680b90, 失败
0027.723ff1b3, 0.8, 92.88%
0034.681f068d, 0.8, 87.21%
0037.d6aaff4d，0.8，88.48%
0051.2a835ef5,0.8, 90%
0064.47c8c813,0.8, 92.52%

0072.85d93835, 91.54%
0074.7487ab80, 92.96%
0070.805a25d7, 92.39%

0077.d952d15f
0079.d06ea601
0085.a7589fa9, 92.73%
0099.f94e3d49, 92.29%

Failed
 - 无 kl 不加 无标签 MixMatch 的 loss，0067.0e946138
 - ，0071.1714cc1a,0011.82b17af3

 - 0013.bbb13496，直接到0

测试 semi_sche
    0086.8298d1e7
    0087.05eaa28b


cifar100
0021.65d2213a, 0.8  failed,
0048.40efcc11, 0.8  failed,
"""

if __name__ == '__main__':
    import os
    import sys

    chdir = os.path.dirname(os.path.abspath(__file__))
    chdir = os.path.dirname(chdir)
    chdir = os.path.dirname(chdir)
    sys.path.append(chdir)

from arch.meta import MetaWideResNet, MetaSGD
import torch
from torch import autograd
from typing import List, Tuple
from thexp import Trainer, Meter, Params, AvgMeter
from trainers import NoisyParams, GlobalParams
from torch.nn import functional as F
from trainers.mixin import *
from arch.meta import MetaModule
import numpy as np


class IEGParams(NoisyParams):

    def __init__(self):
        super().__init__()
        self.epoch = 550
        self.batch_size = 100
        self.K = 1
        self.mix_beta = 0.5
        self.T = 0.5
        self.burn_in_epoch = 0
        self.loss_p_percentile = 0.7
        self.optim = self.create_optim('SGD',
                                       lr=0.1,
                                       momentum=0.9,
                                       weight_decay=1e-4,
                                       nesterov=True)
        self.noisy_ratio = 0.8
        self.ema_alpha = 0.999
        self.consistency_factor = 20
        self.pred_thresh = 0.75
        self.widen_factor = 2  # 10 needs multi-gpu

        self.lub = False
        self.lkl = True

    def initial(self):
        super(IEGParams, self).initial()
        if params.dataset == 'cifar10':
            self.lr_sche = self.SCHE.Cos(start=self.optim.args.lr, end=0.0002, left=0, right=params.epoch - 50)
        else:
            self.lr_sche = self.SCHE.Cos(start=self.optim.args.lr, end=0.002, left=0, right=params.epoch)

        self.epoch_step = self.SCHE.Linear(end=100, right=self.epoch)
        self.init_eps_val = 1. / self.batch_size
        self.grad_eps_init = 0.9  # eps for meta learning init value
        self.corr_sigmoid_sche = self.SCHE.Cos(5, 10, right=self.epoch)
        self.pthresh_sche = self.SCHE.Cos(0.9, 0.7, right=self.epoch)
        self.gmm_sche = self.SCHE.Cos(0.9, 0.5, right=self.epoch // 2)
        self.loc_sche = self.SCHE.Cos(0.5, 1, right=self.epoch // 2)

        self.semi_sche = self.SCHE.Cos(1, 0, right=self.epoch // 8)

        self.val_size = 5000
        if self.dataset == 'cifar100':
            self.query_size = 1000
            self.pred_thresh = 0.95
            self.wideresnet28_10()
        elif self.dataset == 'cifar10':
            self.wideresnet282()
            self.query_size = 100


class IEGTrainer(datasets.IEGSyntheticNoisyMixin,
                 callbacks.BaseCBMixin, callbacks.callbacks.TrainCallback,
                 models.BaseModelMixin,
                 acc.ClassifyAccMixin,
                 losses.CELoss, losses.MixMatchLoss, losses.IEGLoss,
                 Trainer):
    priority = -1

    def callbacks(self, params: IEGParams):
        super(IEGTrainer, self).callbacks(params)
        self.hook(self)
        self.logger.info(self._callback_set)

    def on_initial_end(self, trainer: Trainer, func, params: NoisyParams, meter: Meter, *args, **kwargs):
        self.target_mem = torch.zeros(self.train_size, device=self.device, dtype=torch.float)
        self.weight_mem = torch.zeros(self.train_size, device=self.device, dtype=torch.bool)

        self.plabel_mem = torch.zeros(self.train_size, params.n_classes, device=self.device, dtype=torch.float)
        self.noisy_cls_mem = torch.zeros(self.train_size, dtype=torch.float, device=self.device)
        self.noisy_cls = torch.zeros(self.train_size, dtype=torch.float, device=self.device)
        self.true_pred_mem = torch.zeros(self.train_size, dtype=torch.float, device=self.device)
        self.false_pred_mem = torch.zeros(self.train_size, params.epoch, dtype=torch.float, device=self.device)
        self.corrcoefs = torch.zeros(self.train_size, dtype=torch.float, device=self.device)
        self.clean_mean_prob = 0
        self.gmm_model = None

    def on_train_epoch_end(self, trainer: 'IEGTrainer', func, params: IEGParams, meter: Meter, *args, **kwargs):
        with torch.no_grad():
            from sklearn import metrics
            f_mean = self.false_pred_mem[:, max(params.eidx - params.gmm_burnin, 0):params.eidx].mean(
                dim=1).cpu().numpy()
            f_cur = self.false_pred_mem[:, params.eidx - 1].cpu().numpy()
            feature = np.stack([f_mean, f_cur], axis=1)

            model = tricks.group_fit(feature)
            noisy_cls = model.predict_proba(feature)[:, 0]  # type:np.ndarray

            if params.eidx == params.gmm_burnin:
                self.noisy_cls_mem = torch.tensor(noisy_cls, device=self.device)

            # true_ncls = (self.true_pred_mem == self.false_pred_mem[:, params.eidx - 1]).cpu().numpy()
            true_ncls = (self.true_pred_mem == self.false_pred_mem[:, params.eidx - 1])
            m = Meter()
            if params.eidx > params.gmm_burnin:
                self.noisy_cls_mem = torch.tensor(noisy_cls, device=self.device) * 0.1 + self.noisy_cls_mem * 0.9
                # self.noisy_cls = 1 / (1 + torch.exp(-(10 * self.noisy_cls_mem.clone() - 5)))
                self.noisy_cls = self.noisy_cls_mem.clone()

                # self.noisy_cls[self.noisy_cls] = 0

                # 随时间推移，越难以区分的样本越应该直接挂掉，而不是模糊来模糊去的加权（或许）
                # self.noisy_cls[self.noisy_cls >= 0.5].clamp_min_(1)
                m.gmm_t = self.noisy_cls[true_ncls].float().mean()
                m.percent(m.gmm_t_)
                m.gmm_f = self.noisy_cls[true_ncls.logical_not()].float().mean()
                m.percent(m.gmm_f_)

            if params.eidx > params.burnin:
                x = self.false_pred_mem[:, 1:params.eidx - 1].cpu()
                y = torch.arange(x.shape[-1]).repeat([x.shape[0], 1]).float()
                corrcoefs = tricks.bcorrcoef(x, y).to(self.device)
                corrcoefs[torch.isnan(corrcoefs)] = 0
                mask = corrcoefs > 0
                corrcoefs[mask] = corrcoefs[mask] / corrcoefs[mask].max()
                mask.logical_not_()
                corrcoefs[mask] = corrcoefs[mask] / -corrcoefs[mask].min()

                self.corrcoefs = 1 - (1 / (1 + torch.exp(-(params.corr_sigmoid_sche(params.eidx) * corrcoefs))))

                m.cor_t = self.corrcoefs[true_ncls].mean()
                m.percent(m.cor_t_)
                m.cor_f = self.corrcoefs[true_ncls.logical_not()].mean()
                m.percent(m.cor_f_)

            meter.update(m)
            self.logger.info(m)

    def initial(self):
        super().initial()

    def unsupervised_loss(self,
                          xs: torch.Tensor, axs: torch.Tensor,
                          vxs: torch.Tensor, vys: torch.Tensor,
                          logits_lis: List[torch.Tensor],
                          meter: Meter):
        '''create Lub, Lpb, Lkl'''

        logits_lis = [self.logit_norm_(logits) for logits in logits_lis]

        p_target = self.label_guesses_(*logits_lis)
        p_target = self.sharpen_(p_target, params.T)

        re_v_targets = tricks.onehot(vys, params.n_classes)
        mixed_input, mixed_target = self.mixmatch_up_(vxs, [axs], re_v_targets, p_target,
                                                      beta=params.mix_beta)

        mixed_logits = self.to_logits(mixed_input)
        mixed_logits_lis = mixed_logits.split_with_sizes([vxs.shape[0], axs.shape[0]])
        (mixed_v_logits, mixed_nn_logits) = [self.logit_norm_(l) for l in mixed_logits_lis]  # type:torch.Tensor

        # mixed_nn_logits = torch.cat([mixed_n_logits, mixed_an_logits], dim=0)
        mixed_v_targets, mixed_nn_targets = mixed_target.split_with_sizes(
            [mixed_v_logits.shape[0], mixed_nn_logits.shape[0]])

        # Lpβ，验证集作为半监督中的有标签数据集
        meter.Lall = meter.Lall + self.loss_ce_with_targets_(mixed_v_logits, mixed_v_targets,
                                                             meter=meter, name='Lpb') * params.semi_sche(params.eidx)
        # p * Luβ，训练集作为半监督中的无标签数据集
        if params.lub:
            meter.Lall = meter.Lall + self.loss_ce_with_targets_(mixed_nn_logits, mixed_nn_targets,
                                                                 meter=meter,
                                                                 name='Lub') * params.semi_sche(params.eidx)

        # Lkl，对多次增广的一致性损失

        return p_target

    def train_batch(self, eidx, idx, global_step, batch_data, params: IEGParams, device: torch.device):
        meter = Meter()
        train_data, (vxs, vys) = batch_data  # type:List[torch.Tensor],(torch.Tensor,torch.Tensor)

        ids = train_data[0]
        axs = train_data[1]
        xs = torch.cat(train_data[2:2 + params.K])
        ys, nys = train_data[-2:]  # type:torch.Tensor

        w_logits = self.to_logits(xs)
        aug_logits = self.to_logits(axs)

        logits = w_logits.chunk(params.K)[0]
        # logits = aug_logits  # .chunk(params.K)[0]

        w_targets = torch.softmax(w_logits.chunk(params.K)[0], dim=1).detach()
        guess_targets = self.unsupervised_loss(xs, axs, vxs, vys,
                                               logits_lis=[*w_logits.detach().chunk(params.K)],
                                               meter=meter)
        # guess_targets = self.sharpen_(torch.softmax(logits, dim=1))

        # label_pred = guess_targets.gather(1, nys.unsqueeze(dim=1)).squeeze()

        fweight = torch.ones(logits.shape[0], device=device)
        if eidx > params.burnin:
            fweight -= self.noisy_cls[ids]
            fweight -= self.corrcoefs[ids]
            fweight = torch.relu(fweight)

        raw_targets = w_targets  # guess_targets  # torch.softmax(w_logits, dim=1)

        with torch.no_grad():
            targets = self.plabel_mem[ids] * params.targets_ema + raw_targets * (1 - params.targets_ema)
            self.plabel_mem[ids] = targets
        values, p_labels = targets.max(dim=1)

        mask = values > params.pthresh_sche(eidx)
        if mask.any():
            self.acc_precise_(p_labels[mask], ys[mask], meter, name='pacc')
        mask = mask.float()

        meter.pm = mask.mean()

        meter.Lall = meter.Lall + self.loss_ce_with_masked_(logits, nys, fweight,
                                                            meter=meter)

        meter.Lall = meter.Lall + self.loss_ce_with_masked_(logits, p_labels,
                                                            (1 - fweight) * mask,
                                                            meter=meter,
                                                            name='Lpce')
        if params.lkl:
            meter.Lall = meter.Lall + self.loss_kl_ieg_(logits, aug_logits,
                                                        n_classes=params.n_classes,
                                                        consistency_factor=params.consistency_factor,
                                                        meter=meter)

        meter.tw = fweight[ys == nys].mean()
        meter.fw = fweight[ys != nys].mean()
        # meter.tc = self.corrcoefs[ids][ys == nys].mean()
        # meter.fc = self.corrcoefs[ids][ys != nys].mean()

        if 'Lall' in meter:
            self.optim.zero_grad()
            meter.Lall.backward()
            self.optim.step()

        self.acc_precise_(w_targets.argmax(dim=1), ys, meter, name='true_acc')
        n_mask = nys != ys
        if n_mask.any():
            self.acc_precise_(w_targets.argmax(dim=1)[n_mask], nys[n_mask], meter, name='noisy_acc')

        with torch.no_grad():
            false_pred = guess_targets.gather(1, nys.unsqueeze(dim=1)).squeeze()  # [ys != nys]
            true_pred = guess_targets.gather(1, ys.unsqueeze(dim=1)).squeeze()  # [ys != nys]
            self.true_pred_mem[ids] = true_pred
            self.false_pred_mem[ids, eidx - 1] = false_pred

        return meter

    def to_logits(self, xs) -> torch.Tensor:
        return self.model(xs)


if __name__ == '__main__':
    params = IEGParams()
    params.device = 'cuda:0'
    params.from_args()
    params.K = 2

    params.burnin = 3
    params.gmm_burnin = 10
    params.noisy_ratio = 0.8
    params.targets_ema = 0.3
    # params.tolerance_type = 'exp'

    params.filter_ema = 0.999

    trainer = IEGTrainer(params)
    trainer.test()
    trainer.train()
    trainer.save_checkpoint()
