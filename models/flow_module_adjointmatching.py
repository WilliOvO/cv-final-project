from typing import Any
import torch
import time
import os
import random
import wandb
import numpy as np
import pandas as pd
import logging
import torch.distributed as dist
from pytorch_lightning import LightningModule
from analysis import metrics 
from analysis import utils as au
from models.flow_model import FlowModel
from models import utils as mu
from data.interpolant import Interpolant 
from data import utils as du
from data import all_atom
from data import so3_utils
from data import residue_constants
from experiments import utils as eu
from pytorch_lightning.loggers.wandb import WandbLogger


class FlowModule(LightningModule):

    def __init__(self, cfg):
        super().__init__()
        self._print_logger = logging.getLogger(__name__)
        self._exp_cfg = cfg.experiment
        self._model_cfg = cfg.model
        self._data_cfg = cfg.data
        self._interpolant_cfg = cfg.interpolant

        # Set-up vector field prediction model
        self.model = FlowModel(cfg.model)

        # Set-up interpolant
        self.interpolant = Interpolant(cfg.interpolant)

        self.validation_epoch_metrics = []
        self.validation_epoch_samples = []
        self.save_hyperparameters()

        self._checkpoint_dir = None
        self._inference_dir = None
    
    def solve_adjoint_state(self, drift, x_t, t, mask):
        """
        Solve lean adjoint ODE following HJB equation 
        Args:
            drift: Drift function (callable)
            x_t: Current state [batch_size, num_residues, 3]
            t: Current time [batch_size, 1]
            mask: Loss mask [batch_size, num_residues]
        """
        # Initialize at terminal time with proper final condition
        a_t = -torch.ones_like(x_t)  # [batch_size, num_residues, 3]
        a_t = a_t * mask[..., None]  # Apply mask while keeping dimensions

        # Backward integration timesteps
        timesteps = torch.linspace(1.0, 0.0, 20, device=a_t.device)
        dt = timesteps[1] - timesteps[0]

        curr_x = x_t.clone()
        adjoint_states = [a_t]

        # Backward integration of HJB adjoint equation
        for t_curr, t_next in zip(timesteps[:-1], timesteps[1:]):
            # Match time dimensions with batch
            t_curr = t_curr.expand_as(t)  # [batch_size, 1]

            # Compute drift with proper broadcasting
            curr_drift = drift(curr_x, t_curr) * mask[..., None]  # [batch_size, num_residues, 3]

            # Get drift gradient w.r.t state
            grad_b = torch.autograd.grad(
                curr_drift.sum(), curr_x,  
                create_graph=True, retain_graph=True
            )[0]  # [batch_size, num_residues, 3, 3]

            # HJB adjoint equation discretization
            a_t = a_t - dt * torch.einsum('...ijm,...j->...i', grad_b, a_t)  
            a_t = a_t * mask[..., None]
            adjoint_states.append(a_t)

            # Update state with controlled dynamics
            with torch.no_grad():
                dx = dt * curr_drift
                curr_x = curr_x + dx

        return adjoint_states[-1]

    @property
    def checkpoint_dir(self):
        if self._checkpoint_dir is None:
            if dist.is_initialized():
                if dist.get_rank() == 0:
                    checkpoint_dir = [self._exp_cfg.checkpointer.dirpath]
                else:
                    checkpoint_dir = [None]
                dist.broadcast_object_list(checkpoint_dir, src=0)
                checkpoint_dir = checkpoint_dir[0]
            else:
                checkpoint_dir = self._exp_cfg.checkpointer.dirpath
            self._checkpoint_dir = checkpoint_dir
            os.makedirs(self._checkpoint_dir, exist_ok=True)
        return self._checkpoint_dir

    @property
    def inference_dir(self):
        if self._inference_dir is None:
            if dist.is_initialized():
                if dist.get_rank() == 0:
                    inference_dir = [self._exp_cfg.inference_dir]
                else:
                    inference_dir = [None]
                dist.broadcast_object_list(inference_dir, src=0)
                inference_dir = inference_dir[0]
            else:
                inference_dir = self._exp_cfg.inference_dir
            self._inference_dir = inference_dir
            os.makedirs(self._inference_dir, exist_ok=True)
        return self._inference_dir

    def on_train_start(self):
        self._epoch_start_time = time.time()
        
    def on_train_epoch_end(self):
        epoch_time = (time.time() - self._epoch_start_time) / 60.0
        self.log(
            'train/epoch_time_minutes',
            epoch_time,
            on_step=False,
            on_epoch=True,
            prog_bar=False
        )
        self._epoch_start_time = time.time()

    def model_step(self, noisy_batch: Any):
        training_cfg = self._exp_cfg.training
        loss_mask = noisy_batch['res_mask'] * noisy_batch['diffuse_mask'] 
        if torch.any(torch.sum(loss_mask, dim=-1) < 1):
            raise ValueError('Empty batch encountered')

        # Get model predictions
        model_output = self.model(noisy_batch)
        pred_trans_1 = model_output['pred_trans']
        pred_rotmats_1 = model_output['pred_rotmats']

        # Calculate validity reward
        # Convert predictions to atom positions for validity check
        pred_bb_atoms = all_atom.to_atom37(pred_trans_1, pred_rotmats_1)[:, :, :3]

        bond_lengths = torch.norm(pred_bb_atoms[:,:,1:] - pred_bb_atoms[:,:,:-1], dim=-1)
        valid_bonds = torch.logical_and(bond_lengths > 1.0, bond_lengths < 2.0) # Reasonable bond length range in Angstroms
        validity_reward = valid_bonds.float().mean(dim=-1)

        batch_size = pred_bb_atoms.shape[0]
        flat_coords = pred_bb_atoms.reshape(batch_size, -1, 3)

        pairwise_dists = torch.cdist(flat_coords, flat_coords)

        diversity_reward = pairwise_dists.mean(dim=(-1,-2))

        # Combined reward with weights
        reward = 2.0 * diversity_reward + 1.0 * validity_reward

        # Get current states and time
        trans_t = noisy_batch['trans_t']
        rotmats_t = noisy_batch['rotmats_t']
        r3_t = noisy_batch['r3_t']
        so3_t = noisy_batch['so3_t']

        # Compute memoryless noise schedule
        alpha_t = r3_t
        beta_t = 1 - r3_t
        alpha_dot = torch.ones_like(r3_t) 
        beta_dot = -torch.ones_like(r3_t)
        eta_t = beta_t * (alpha_dot/alpha_t * beta_t - beta_dot)
        sigma_t = torch.sqrt(2 * eta_t)

        # Scale losses by reward
        def trans_drift(x, t):
            return (pred_trans_1 - x) / (1 - t[..., None]) * reward[..., None, None]

        trans_adjoint = self.solve_adjoint_state(
            trans_drift, trans_t, r3_t, loss_mask)

        trans_vf = trans_drift(trans_t, r3_t)
        trans_adjoint_term = trans_vf + sigma_t[..., None, None] * trans_adjoint

        trans_loss = training_cfg.translation_loss_weight * torch.sum(
            trans_adjoint_term**2 * loss_mask[..., None],
            dim=(-1, -2)
        ) / torch.sum(loss_mask, dim=-1)
        trans_loss = torch.clamp(trans_loss, max=5)

        def rot_drift(r, t):
            return so3_utils.calc_rot_vf(r, pred_rotmats_1) * reward[..., None, None]

        rot_adjoint = self.solve_adjoint_state(
            rot_drift, rotmats_t, so3_t, loss_mask)

        rot_vf = rot_drift(rotmats_t, so3_t)
        rot_sigma_t = torch.sqrt(2 * so3_t)
        rot_adjoint_term = rot_vf + rot_sigma_t[..., None, None] * rot_adjoint

        rots_vf_loss = training_cfg.rotation_loss_weights * torch.sum(
            rot_adjoint_term**2 * loss_mask[..., None],
            dim=(-1, -2)
        ) / torch.sum(loss_mask, dim=-1)

        # Keep auxiliary losses but scale them by reward
        auxiliary_loss = 0.0
        if training_cfg.aux_loss_weight > 0 and r3_t[0, 0] > 0.5:
            gt_bb_atoms = all_atom.to_atom37(
                noisy_batch['trans_1'],
                noisy_batch['rotmats_1']
            )[:, :, :3]

            loss_denom = torch.sum(loss_mask, dim=-1) * 3
            bb_atom_loss = torch.sum(
                (gt_bb_atoms - pred_bb_atoms)**2 * loss_mask[..., None, None],
                dim=(-1, -2, -3)
            ) / loss_denom

            auxiliary_loss = bb_atom_loss * training_cfg.aux_loss_weight * reward
            auxiliary_loss = torch.clamp(auxiliary_loss, max=5)

        se3_vf_loss = trans_loss + rots_vf_loss + auxiliary_loss

        if torch.any(torch.isnan(se3_vf_loss)):
            raise ValueError('NaN loss encountered')

        return {
            "trans_loss": trans_loss,
            "auxiliary_loss": auxiliary_loss, 
            "rots_vf_loss": rots_vf_loss,
            "se3_vf_loss": se3_vf_loss,
            "diversity_reward": diversity_reward,
            "validity_reward": validity_reward,
            "total_reward": reward
        }

    def validation_step(self, batch: Any, batch_idx: int):
        res_mask = batch['res_mask']
        self.interpolant.set_device(res_mask.device)
        num_batch, num_res = res_mask.shape
        diffuse_mask = batch['diffuse_mask']
        csv_idx = batch['csv_idx']
        atom37_traj, _, _ = self.interpolant.sample(
            num_batch,
            num_res,
            self.model,
            trans_1=batch['trans_1'],
            rotmats_1=batch['rotmats_1'],
            diffuse_mask=diffuse_mask,
            chain_idx=batch['chain_idx'],
            res_idx=batch['res_idx'],
        )
        samples = atom37_traj[-1].numpy()
        batch_metrics = []
        for i in range(num_batch):
            sample_dir = os.path.join(
                self.checkpoint_dir,
                f'sample_{csv_idx[i].item()}_idx_{batch_idx}_len_{num_res}'
            )
            os.makedirs(sample_dir, exist_ok=True)

            # Write out sample to PDB file
            final_pos = samples[i]
            saved_path = au.write_prot_to_pdb(
                final_pos,
                os.path.join(sample_dir, 'sample.pdb'),
                no_indexing=True
            )
            if isinstance(self.logger, WandbLogger):
                self.validation_epoch_samples.append(
                    [saved_path, self.global_step, wandb.Molecule(saved_path)]
                )

            mdtraj_metrics = metrics.calc_mdtraj_metrics(saved_path)
            ca_idx = residue_constants.atom_order['CA']
            ca_ca_metrics = metrics.calc_ca_ca_metrics(final_pos[:, ca_idx])
            batch_metrics.append((mdtraj_metrics | ca_ca_metrics))

        batch_metrics = pd.DataFrame(batch_metrics)
        self.validation_epoch_metrics.append(batch_metrics)
        
    def on_validation_epoch_end(self):
        if len(self.validation_epoch_samples) > 0:
            self.logger.log_table(
                key='valid/samples',
                columns=["sample_path", "global_step", "Protein"],
                data=self.validation_epoch_samples)
            self.validation_epoch_samples.clear()
        val_epoch_metrics = pd.concat(self.validation_epoch_metrics)
        for metric_name,metric_val in val_epoch_metrics.mean().to_dict().items():
            self._log_scalar(
                f'valid/{metric_name}',
                metric_val,
                on_step=False,
                on_epoch=True,
                prog_bar=False,
                batch_size=len(val_epoch_metrics),
            )
        self.validation_epoch_metrics.clear()

    def _log_scalar(
            self,
            key,
            value,
            on_step=True,
            on_epoch=False,
            prog_bar=True,
            batch_size=None,
            sync_dist=False,
            rank_zero_only=True
        ):
        if sync_dist and rank_zero_only:
            raise ValueError('Unable to sync dist when rank_zero_only=True')
        self.log(
            key,
            value,
            on_step=on_step,
            on_epoch=on_epoch,
            prog_bar=prog_bar,
            batch_size=batch_size,
            sync_dist=sync_dist,
            rank_zero_only=rank_zero_only
        )

    def training_step(self, batch: Any, stage: int):
        step_start_time = time.time()
        self.interpolant.set_device(batch['res_mask'].device)
        noisy_batch = self.interpolant.corrupt_batch(batch)
        if self._interpolant_cfg.self_condition and random.random() > 0.5:
            with torch.no_grad():
                model_sc = self.model(noisy_batch)
                noisy_batch['trans_sc'] = (
                    model_sc['pred_trans'] * noisy_batch['diffuse_mask'][..., None]
                    + noisy_batch['trans_1'] * (1 - noisy_batch['diffuse_mask'][..., None])
                )
        batch_losses = self.model_step(noisy_batch)
        num_batch = batch_losses['trans_loss'].shape[0]
        total_losses = {
            k: torch.mean(v) for k,v in batch_losses.items()
        }
        for k,v in total_losses.items():
            self._log_scalar(
                f"train/{k}", v, prog_bar=False, batch_size=num_batch)
        
        # Losses to track. Stratified across t.
        so3_t = torch.squeeze(noisy_batch['so3_t'])
        self._log_scalar(
            "train/so3_t",
            np.mean(du.to_numpy(so3_t)),
            prog_bar=False, batch_size=num_batch)
        r3_t = torch.squeeze(noisy_batch['r3_t'])
        self._log_scalar(
            "train/r3_t",
            np.mean(du.to_numpy(r3_t)),
            prog_bar=False, batch_size=num_batch)
        for loss_name, loss_dict in batch_losses.items():
            if loss_name == 'rots_vf_loss':
                batch_t = so3_t
            else:
                batch_t = r3_t
            stratified_losses = mu.t_stratified_loss(
                batch_t, loss_dict, loss_name=loss_name)
            for k,v in stratified_losses.items():
                self._log_scalar(
                    f"train/{k}", v, prog_bar=False, batch_size=num_batch)

        # Training throughput
        scaffold_percent = torch.mean(batch['diffuse_mask'].float()).item()
        self._log_scalar(
            "train/scaffolding_percent",
            scaffold_percent, prog_bar=False, batch_size=num_batch)
        motif_mask = 1 - batch['diffuse_mask'].float()
        num_motif_res = torch.sum(motif_mask, dim=-1)
        self._log_scalar(
            "train/motif_size", 
            torch.mean(num_motif_res).item(), prog_bar=False, batch_size=num_batch)
        self._log_scalar(
            "train/length", batch['res_mask'].shape[1], prog_bar=False, batch_size=num_batch)
        self._log_scalar(
            "train/batch_size", num_batch, prog_bar=False)
        step_time = time.time() - step_start_time
        self._log_scalar(
            "train/examples_per_second", num_batch / step_time)
        train_loss = total_losses['se3_vf_loss']
        self._log_scalar(
            "train/loss", train_loss, batch_size=num_batch)
        self._log_scalar("train/diversity_reward", torch.mean(batch_losses['diversity_reward']), batch_size=num_batch)
        self._log_scalar("train/validity_reward", torch.mean(batch_losses['validity_reward']), batch_size=num_batch) 
        self._log_scalar("train/total_reward", torch.mean(batch_losses['total_reward']), batch_size=num_batch)
        return train_loss

    def configure_optimizers(self):
        return torch.optim.AdamW(
            params=self.model.parameters(),
            **self._exp_cfg.optimizer
        )

    def predict_step(self, batch, batch_idx):
        del batch_idx # Unused
        device = f'cuda:{torch.cuda.current_device()}'
        interpolant = Interpolant(self._infer_cfg.interpolant) 
        interpolant.set_device(device)

        sample_ids = batch['sample_id'].squeeze().tolist()
        sample_ids = [sample_ids] if isinstance(sample_ids, int) else sample_ids
        num_batch = len(sample_ids)

        if 'diffuse_mask' in batch: # motif-scaffolding
            target = batch['target'][0]
            trans_1 = batch['trans_1']
            rotmats_1 = batch['rotmats_1']
            diffuse_mask = batch['diffuse_mask']
            true_bb_pos = all_atom.atom37_from_trans_rot(trans_1, rotmats_1, 1 - diffuse_mask)
            true_bb_pos = true_bb_pos[..., :3, :].reshape(-1, 3).cpu().numpy()
            _, sample_length, _ = trans_1.shape
            sample_dirs = [os.path.join(
                self.inference_dir, target, f'sample_{str(sample_id)}')
                for sample_id in sample_ids]
        else: # unconditional
            sample_length = batch['num_res'].item()
            true_bb_pos = None
            sample_dirs = [os.path.join(
                self.inference_dir, f'length_{sample_length}', f'sample_{str(sample_id)}')
                for sample_id in sample_ids]
            trans_1 = rotmats_1 = diffuse_mask = None
            diffuse_mask = torch.ones(1, sample_length, device=device)

        # Sample batch
        atom37_traj, model_traj, _ = interpolant.sample(
            num_batch, sample_length, self.model,
            trans_1=trans_1, rotmats_1=rotmats_1, diffuse_mask=diffuse_mask
        )

        bb_trajs = du.to_numpy(torch.stack(atom37_traj, dim=0).transpose(0, 1))
        for i in range(num_batch):
            sample_dir = sample_dirs[i]
            bb_traj = bb_trajs[i]
            os.makedirs(sample_dir, exist_ok=True)
            if 'aatype' in batch:
                aatype = du.to_numpy(batch['aatype'].long())[0]
            else:
                aatype = np.zeros(sample_length, dtype=int)
            _ = eu.save_traj(
                bb_traj[-1],
                bb_traj,
                np.flip(du.to_numpy(torch.concat(model_traj, dim=0)), axis=0),
                du.to_numpy(diffuse_mask)[0],
                output_dir=sample_dir,
                aatype=aatype,
            )