# Incorporating abstractions into ConPoLe

The learning algorithms are implemented in Python 3.9, using PyTorch. You can install
all Python dependencies using:

```
pip install -r requirements.txt
```
Make sure to also clone the `mathematical-abstractions` submodule while cloning this repository:
```
git clone -b abstract git@github.com:uranium11010/socratic-tutor
git submodule init mathematical-abstractions/
git submodule update mathematical-abstractions/
```
(Note: We won't be using the `variational-item-response-theory-public` submodule, so there's no need to do `git clone --recursive` to clone both submodules.)

## Educational domains (Rust environments)

The environments have a fast Rust implementation in the `commoncore` directory,
which can be easily called from the Python learning agents thanks to [https://github.com/PyO3/pyo3](https://github.com/PyO3/pyo3).
To set them up, follow the steps below:

* First, install a recent Rust compiler (1.50+). The easiest way to do it is with rustup. Simply
  visit [https://rustup.rs/](https://rustup.rs/) and follow the instructions there. To check your
  installation, run `rustc --version` in the command line: you should get something like
  `rustc 1.51.0 (2fd73fabe 2021-03-23)`.
* Then, compile the dynamic library:

```
$ cd /path/to/socratic-tutor/commoncore
$ cargo build --release
```

  This might take a few minutes. It should download all dependencies and then compile our library.
  If all goes well, you should find the library at `target/release/libcommoncore.so`
  (or the equivalent extension in your operating system, e.g. dylib for Mac).
* Finally, we only need to place that library in a location that we can import from Python.
  Simply create a symbolic link at the root of the project and named `commoncore.so`, that points
  to the compiled library:

```
user@machine:~/socratic-tutor$ ln -s commoncore/target/release/libcommoncore.so ./commoncore.so
```

And that's it! If you open a Python shell, you should be able to directly `import commoncore`.
Also, now that you set up the symlink, if you compile the Rust library again (e.g. after it was updated),
we'll automatically pick up the latest version from Python.

## Abstractions

"Abstractions" are what we call patterns that frequently appear among solutions generated by ConPoLe. The submodule `mathematical-abstractions` contains code that discovers abstractions from a dataset of ConPoLe solutions. The README there provides instructions on how to generate these abstractions.

Incorporating abstractions into the ConPoLe environment allows the agent to take a sequence of steps specified by an abstraction as a single action. As such, we hope to reduce the search depth, generate more human-readable solutions, and allow the agent to solve more complex equations.

Currently, there are three kinds of abstractions that have been implemented:
* `ax_seq`: abstractions that only specify a sequence of axioms. 
* `dfs_idx_rel_pos`: Abstractions that incorporate relative position information using the old ConPoLe DFS indexing (file ending in `-pos.json`). 
* `tree_rel_pos`: Abstractions that specify a sequence of axioms and additionally incorporate information about axioms' relative position of application within the expression tree. 
The first two kinds can no longer be incorporated into the ConPoLe environment since their interfaces are outdated.

Example files containing abstractions have been placed under `mathemematical-abstractions/abstractions`. Currently, only those files ending in `-tree.json` (`tree_rel_pos` abstractions) are guaranteed to work with the ConPoLe environment.

To incorporate `tree_rel_pos` abstractions into the ConPoLe environment, specify
```
"abstractions": {
  "path": "path/to/file/with/abstractions",
  "tree_idx": true
}
```
in the config file passed to `agent.py` (see below). (*Note:* The key `tree_idx` refers to `tree_rel_pos` abstractions, whereas the key `consider_pos` refers to `dfs_idx_rel_pos` abstractions. Specifying neither option would use `ax_seq` abstractions.)

## Learning agents

Several learning algorithms are implemented to learn the domains.
They are all in `agent.py`, which is a file that also implements evaluation.

To perform training and evaluation, we use `agent.py`. Run the following command:
```
python agent.py [-h] --config CONFIG [--learn] [--experiment] [--eval] [--eval-checkpoints] [--debug] [--range RANGE] [--gpu GPU]
```

- `--config`: Path to configuration file, or inline JSON. A template configuration file for the `--learn` mode is given in [`template_config.txt`](template_config.txt). (Note: The template currently does not comprehensively list all possible configurations.)
- `--learn`: Put an agent to learn from the environment.
- `--experiment`: Run a batch of experiments with multiple agents and environments.
- `--eval`: Evaluate a learned policy.
- `--eval-checkpoints`: Show the evolution of a learned policy during interaction.
- `--debug`: Enable debug messages.
- `--range RANGE`: Range of experiments to run. Format: 2-5 means range [2, 5).Used to split experiments across multiple machines. Default: all.
- `--gpu GPU`: Which GPU to use (e.g. `"cuda:0"`); defaults to CPU if none is specified.

`--learn` is used to run a single experiment (one agent on one domain), whereas `--experiment` is used to run a batch of experiments (e.g., multiple agents on multiple domains with multiple runs in each configuration). You almost surely want to use `--experiment` since it is more general, even if to perform a single run.

In this abstraction project, we will always be focusing on the `equations-ct` domain and the `NCE` (ConPoLe) learning agent. Here is an example complete config file to run `--experiment` (single run) without abstractions (i.e., original ConPoLe):

```json
{
  "experiment_id": "test",
  "domains": ["equations-ct"],
  "environment_backend": "Rust",
  "wandb_project": "test",
  "gpus": [0],
  "n_runs": 1,
  "agents": [
    {
      "type": "NCE",
      "name": "ConPoLe",
      "n_future_states": 1,
      "replay_buffer_size": 100000,
      "max_depth": 30,
      "beam_size": 10,
      "initial_depth": 8,
      "depth_step": 1,
      "optimize_every": 16,
      "n_gradient_steps": 128,
      "keep_optimizer": true,
      "step_every": 10000,
      "n_bootstrap_problems": 100,
      "q_function": {
        "type": "Bilinear",
        "char_emb_dim": 64,
        "hidden_dim": 256,
        "mlp": true,
        "lstm_layers": 2
      }
    }
  ],
  "eval_environment": {
    "evaluate_every": 100000,
    "eval_config": {
      "max_steps": 30,
      "n_problems": 200
    },
    "output_root": "output",
    "max_steps": 10000000,
    "print_every": 10000
  }
}
```

Here are some additional useful options that can be specified:
* `"epsilon"` in agent config (i.e., dictionary in the "`agents`" list): The value of epsilon in epsilon greedy in exploring solutions. Default is 0.
* `"bootstrap_from"` in agent config: The initial bootstrapping strategy for finding solutions at the beginning of training. Default is `RandomQFunction` (i.e., randomly chose next states during search). An alternative is `InverseLength`. Note that boostrapping is disabled if `"load_pretrained"` (load a pretrained model) is specified in `"q_function"` (see below) or "example_solutions" (learn from example solutions) is specified in agent config.
* `"example_solutions"` in agent config: Path to file containing solutions (as list of `Solution` objects of `steps.py`). When specified, the agent will learn from these solutions at the beginning of training. In addition, if `"max_steps"` is not specified in `"eval_environment"`, these are the only examples that the agent will learn from, hence providing the functionality of fine-tuning. An example solution file containing solutions abstracted with `tree_rel_pos` abstractions is located at `mathematical-abstractions/abs_sols/IAP-8k-8len2-tree-1ksol.pkl`.

Here's an example config file that fine-tunes a pretrained model with 1000 abstracted solutions:

```
{
    "experiment_id": "fine_tune",
    "domain": "equations-ct",
    "environment_backend": "Rust",
    "wandb_project": "abs_fine_tune",
    "abstractions": {
        "path": "mathematical-abstractions/abstractions/IAP-8k-8len2-tree.json",
        "tree_idx": true
    },
    "agent": {
        "type": "NCE",
        "name": "ConPoLe",
        "n_future_states": 1,
        "replay_buffer_size": 100000,
        "max_depth": 30,
        "beam_size": 10,
        "initial_depth": 8,
        "depth_step": 1,
        "optimize_every": 20,
        "n_gradient_steps": 128,
        "keep_optimizer": true,
        "step_every": 10000,
	"epsilon": 0.2,
	"bootstrap_from": "InverseLength",
	"n_bootstrap_problems": 100,
	"example_solutions": "mathematical-abstractions/abs_sols/IAP-8k-8len2-tree-1ksol.pkl",
        "q_function": {
	    "load_pretrained": "pretrained/conpole-equations-ct-good.pt",
            "type": "Bilinear",
            "char_emb_dim": 64,
            "hidden_dim": 256,
            "mlp": true,
            "lstm_layers": 2
        }
    },
    "eval_environment": {
        "evaluate_every": 1000,
        "eval_config": {
            "max_steps": 30,
            "n_problems": 100
        },
        "output_root": "output",
        "print_every": 200
    }
}
```
