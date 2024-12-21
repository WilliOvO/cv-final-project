## Installation

Set up your conda environment.

```bash
# Conda environment with dependencies.
conda env create -f fm.yml

# Activate environment
conda activate fm

# Manually need to install torch-scatter.
pip install torch-scatter -f https://data.pyg.org/whl/torch-2.0.0+cu117.html

# Install local package.
# Current directory should be protein-frame-flow/
pip install -e .
```

Datasets and weights are hosted on Zenodo [here](https://zenodo.org/records/12776473?token=eyJhbGciOiJIUzUxMiJ9.eyJpZCI6Ijg2MDUzYjUzLTkzMmYtNDRhYi1iZjdlLTZlMzk0MmNjOGM3NSIsImRhdGEiOnt9LCJyYW5kb20iOiIwNjExMjEwNGJkMDJjYzRjNGRmNzNmZWJjMWU4OGU2ZSJ9.Jo_xXr6-PpOzJHUEAuSmQJK72TMTcI49SStlAVdOHoI2wi1i59FeXnogHvcNioBjGiJtJN7UAxc6Ihuf1d7_eA).
* `preprocessed_pdb.tar.gz` (2.7 GB)
* `weights.tar.gz` (0.6 GB)
* `preprocessed_scope.tar.gz` (0.3 GB)

Next, untar the datasets

```bash
tar -xzvf preprocessed_pdb.tar.gz
tar -xzvf weights.tar.gz
tar -xzvf preprocessed_scope.tar.gz
```

Other datasets are also possible to train on by processing with the `data/process_pdb_files.py` script.
Your directory should now look like this

```bash
├── analysis
├── build
├── configs
├── data
├── experiments
├── media
├── models
├── openfold
├── processed_pdb
├── processed_scope
└── weights
```

## Wandb

Our training relies on logging with wandb. Log in to Wandb and make an account.
Authorize Wandb [here](https://wandb.ai/authorize).

## Training

All training flags are in `configs/base.yaml`. Below is explanation-by-example of the main flags to change. Note you can combine multiple flags in the command.

```bash
# Train on SCOPE
python -W ignore experiments/train_se3_flows.py data.dataset=scope

# Train on hallucination
python -W ignore experiments/train_se3_flows.py data.task=hallucination
