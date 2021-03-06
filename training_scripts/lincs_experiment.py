import sys
sys.path.append('..')

import argparse

import torch

from networks import JointEncoder, RNNEncoder, FinetunedEncoder
from networks import FCDiscriminator

from dataloader import LincsDataSet, LincsSampler

from torch.utils.data import DataLoader, TensorDataset

import pytorch_lightning as pl
from pytorch_lightning.logging import TestTubeLogger
from pytorch_lightning.callbacks import ModelCheckpoint

import numpy as np

from training_scripts.pretrain_mnist_clf import MNISTClassifier

from models import BiAAE, Lat_SAAE, CVAE, VCCA, JMVAE, VIB, UniAAE, SAAE

from rdkit.Chem import Draw
from rdkit import Chem

from rdkit import RDLogger
lg = RDLogger.logger()
lg.setLevel(RDLogger.CRITICAL)

import matplotlib
matplotlib.use('agg')
import matplotlib.pyplot as plt


def make_conditional_experiment(model):
    class CondGenExperiment(model):
        def __init__(self):
            super().__init__(dataset='lincs_rnn')

            rnn = RNNEncoder(out_dim=88)
            rnn.load_state_dict(torch.load('../saved_models/rnn_enc.ckpt', map_location='cuda:1'))
            self.mine_enc = FinetunedEncoder(rnn, out_dim=self.z_dim)
            self.mine_fc = FCDiscriminator(in_dim=2 * self.z_dim)


        def mi_computation(self, batch, z):
            x, y = batch
            z_shuffled = z[np.random.permutation(z.shape[0])]

            x_lat = self.mine_enc(x)

            t = self.mine_fc(torch.cat((x_lat, z), dim=-1))
            et = torch.exp(self.mine_fc(torch.cat((x_lat, z_shuffled), dim=-1)))

            mi_lb = t.mean() - torch.log(et.mean())

            return mi_lb

        def training_step(self, batch, batch_nb, *args):
            stats = super().training_step(batch, batch_nb, *args)

            if len(args) == 0: # VAE-like approaches
                batch = (batch[0], batch[1].detach())

                z = self.get_latents(batch).detach()

                mi = self.mi_computation(batch, z)

                stats['loss'] += -mi
                stats['log']['mi[xz|y]'] = mi
            else: # AAE-like approaches
                if args[0] == 2: # optimizer number
                    batch = (batch[0], batch[1].detach())

                    z = self.get_latents(batch).detach()

                    mi = self.mi_computation(batch, z)

                    stats = {}
                    stats['loss'] = -mi
                    stats['log'] = {}
                    stats['log']['mi[xz|y]'] = mi
                    
            return stats

        def configure_optimizers(self):
            optim = super().configure_optimizers()

            if isinstance(optim, tuple): # AAE like approaches
                mi_params = torch.nn.ModuleList([self.mine_fc, self.mine_enc]).parameters()

                optim[0].append(torch.optim.Adam(mi_params, lr=3e-4))

            return optim

        def validation_step(self, batch, batch_nb):
            # compute MINE on validation stage
            stats = super().validation_step()

            if stats is None:
                stats = {}

            z = self.get_latents(batch)

            mi = self.mi_computation(batch, z)
            stats['mi[xz|y]'] = mi

            x, y = batch
            sampled_x = self.sample(y)
                
            stats['x_sam'] = sampled_x
            stats['x'] = x
            stats['y'] = y
                
            return stats

        def validation_end(self, outputs):
            # compute mean values of validation statistics
            val_stats = {}

            val_stats['val_mi[xz|y]'] = torch.stack([x['mi[xz|y]'] for x in outputs]).mean()
                

            fig = plt.figure(num=0, figsize=(10, 4), dpi=300)
                
            ax = fig.add_subplot(1,1,1)
            ax.axis('off')
            ax.imshow(Draw.MolsToGridImage([Chem.MolFromSmiles(s) for s in outputs[0]['x_sam'][:5]]
                                                     + [Chem.MolFromSmiles(s) for s in outputs[0]['x'][:5]], molsPerRow=5))

            fig.canvas.draw()

            buf = fig.canvas.tostring_rgb()
            ncols, nrows = fig.canvas.get_width_height()
            fig_array = np.fromstring(buf, dtype=np.uint8).reshape(nrows, ncols, 3) / 255.
            fig_array = fig_array.transpose(2, 0, 1)

            self.logger.experiment.add_image('samples', fig_array)

            return {'log': val_stats}

        @pl.data_loader
        def train_dataloader(self):
            dataset = LincsSampler(LincsDataSet('../data/lincs'), test_set=0, 
                                   use_smiles=True)
            dataloader = DataLoader(dataset, batch_size=256, shuffle=True,
                                    drop_last=False)
            return dataloader

        @pl.data_loader
        def val_dataloader(self):
            dataset = LincsSampler(LincsDataSet('../data/lincs'), test_set=1,
                                   use_smiles=True)
            dataloader = DataLoader(dataset, batch_size=256, shuffle=True,
                                    drop_last=False)
            return dataloader

    return CondGenExperiment

if __name__ == '__main__':
    torch.manual_seed(777)

    parser = argparse.ArgumentParser(description='Script to perform benchmark on LINCS datasets')
    parser.add_argument('--model', type=str, default='biaae')
    parser.add_argument('--gpu', type=int, default=-1)

    args = parser.parse_args()

    models_dict = {'biaae': BiAAE, 'lat_saae': Lat_SAAE, 'uniaae': UniAAE,
                   'cvae': CVAE, 'vcca': VCCA, 'jmvae': JMVAE, 'vib': VIB,
                   'saae': SAAE}
    
    model = make_conditional_experiment(models_dict[args.model])()
    
    model_checkpoint = ModelCheckpoint('../saved_models/lincs_rnn_' + args.model, save_best_only=False, period=10)
    
    logger = TestTubeLogger(save_dir='../logs/', name='lincs_rnn_' + args.model)
    tr = pl.Trainer(gpus=([] if (args.gpu < 0) else [args.gpu]),
                    logger=logger,
                    checkpoint_callback=model_checkpoint,
                    early_stop_callback=False,
                    max_nb_epochs=400, show_progress_bar=False)
    tr.fit(model)
    torch.save(model.state_dict(), '../saved_models/lincs_rnn_{}.ckpt'.format(args.model))
