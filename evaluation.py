import datetime
import pickle
import traceback
import hashlib
import os
import json
import subprocess
import copy

import util
from environment import Environment
from q_function import InverseLength, RandomQFunction

import torch
import wandb
from tqdm import tqdm
import numpy as np


class SuccessRatePolicyEvaluator:
    """Evaluates the policy derived from a Q function by its success rate at solving
       problems generated by an environment."""
    def __init__(self, environment, config):
        self.environment = environment
        self.seed = config.get('seed', 0)
        self.n_problems = config.get('n_problems', 100)  # How many problems to use.
        self.max_steps = config.get('max_steps', 30)  # Maximum length of an episode.
        self.beam_size = config.get('beam_size', 1)  # Size of the beam in beam search.
        self.debug = config.get('debug', False)  # Whether to print all steps during evaluation.
        self.save_sols = config.get('save_sols')  # Whether to save solutions

    def evaluate(self, q, verbose=False, show_progress=False):
        successes, failures, solution_lengths = [], [], []
        wrapper = tqdm if show_progress else lambda x: x

        if self.save_sols:
            saved_sols = []
        for i in wrapper(range(self.n_problems)):
            problem = self.environment.generate_new(seed=(self.seed + i))
            success, history = q.rollout(self.environment, problem,
                                         self.max_steps, self.beam_size, self.debug)
            if success:
                successes.append((i, problem))
                if self.save_sols:
                    saved_sols.append(q.recover_solutions(history)[0])
            else:
                failures.append((i, problem))
                if self.save_sols:
                    saved_sols.append(False)
            solution_lengths.append(len(history) - 1 if success else -1)
            if verbose:
                print(i, problem, '-- success?', success)

        np_sol_lens = np.array(solution_lengths)
        results = {
            'success_rate': len(successes) / self.n_problems,
            'solution_lengths': solution_lengths,
            'max_solution_length': max(solution_lengths),
            'mean_solution_length': np.mean(np_sol_lens[np_sol_lens >= 0]).item()
        }
        if self.save_sols:
            saved_res = {'solutions': saved_sols}
            saved_res |= results
            with open(self.save_sols, "wb") as f:
                pickle.dump(saved_res, f)
        results |= {'successes': successes, 'failures': failures}
        return results


class EndOfLearning(Exception):
    '''Exception used to signal the end of the learning budget for an agent.'''


class EnvironmentWithEvaluationProxy:
    '''Wrapper around the environment that triggers an evaluation every K calls'''
    def __init__(self, experiment_id: str, run_index: int, agent_name: str, domain: str,
                 agent, environment: Environment, config: dict = {}):

        self.experiment_id = experiment_id
        self.run_index = run_index
        self.agent_name = agent_name
        self.domain = domain
        self.environment = environment
        self.n_steps = 0

        self.evaluate_every = config.get('evaluate_every')
        self.eval_config = config['eval_config']
        self.agent = agent
        self.max_steps = config.get('max_steps')
        self.print_every = config.get('print_every', 100)

        self.results: list = []
        self.n_new_problems = 0
        self.cumulative_reward = 0
        self.begin_time = datetime.datetime.now()
        self.n_checkpoints = 0

        output_root = os.path.join(config['output_root'], experiment_id, agent_name, domain, f'run{run_index}')
        checkpoint_dir = os.path.join(output_root, 'checkpoints')

        os.makedirs(output_root, exist_ok=True)
        os.makedirs(checkpoint_dir, exist_ok=True)

        self.results_path = os.path.join(output_root, 'results.pkl')
        self.checkpoint_dir = checkpoint_dir

        self.load_checkpoint()

    def load_checkpoint(self):
        'Loads an existing training checkpoint, if available.'
        checkpoint_path = os.path.join(self.checkpoint_dir, 'training-state.pt')

        if os.path.exists(checkpoint_path):
            print('Training checkpoint exists - restoring...')
            device = self.agent.q_function.device
            previous_state = torch.load(checkpoint_path, map_location=device)
            self.agent = previous_state.agent
            self.agent.q_function.to(device)
            self.n_steps = previous_state.n_steps
            self.n_new_problems = previous_state.n_new_problems
            self.cumulative_reward = previous_state.cumulative_reward
            self.n_checkpoints = previous_state.n_checkpoints

    def generate_new(self, domain=None, seed=None):
        self.n_new_problems += 1
        return self.environment.generate_new(domain, seed)

    def step(self, states, domain=None):
        n_steps_before = self.n_steps
        self.n_steps += len(states)

        # If the number of steps crossed the boundary of '0 mod evaluate_every', run evaluation.
        # If the agent took one step at a time, then we would only need to test if
        # n_steps % evaluate_every == 0. However the agent might take multiple steps at once.
        if self.agent.optimize_every is not None and (n_steps_before % self.evaluate_every) + len(states) >= self.evaluate_every:
            self.evaluate()

        if self.max_steps is not None and self.n_steps >= self.max_steps:
            # Budget ended.
            raise EndOfLearning()

        reward_and_actions = self.environment.step(states, domain)
        self.cumulative_reward += sum(rw for rw, _ in reward_and_actions)

        # Same logic as with evaluate_every.
        if self.agent.optimize_every is not None and (n_steps_before % self.print_every) + len(states) >= self.print_every:
            self.print_progress()

        return reward_and_actions

    def evaluate(self):
        print('Evaluating...')
        name, domain = self.agent_name, self.environment.default_domain

        self.environment.test()
        evaluator = SuccessRatePolicyEvaluator(self.environment, self.eval_config)
        results = evaluator.evaluate(self.agent.get_q_function(), show_progress=True)
        self.environment.train()
        results['n_steps'] = self.n_steps
        results['experiment_id'] = self.experiment_id
        results['run_index'] = self.run_index
        results['name'] = name
        results['domain'] = domain
        results['problems_seen'] = self.n_new_problems
        results['cumulative_reward'] = self.cumulative_reward

        wandb.log({'success_rate': results['success_rate'],
                   'problems_seen': results['problems_seen'],
                   'n_environment_steps': results['n_steps'],
                   'cumulative_reward': results['cumulative_reward'],
                   'max_solution_length': results['max_solution_length'],
                   'mean_solution_length': results['mean_solution_length']
                   })

        print(util.now(), f'Success rate ({name}-{domain}-run{self.run_index}):',
                results['success_rate'], '\tMax length:', results['max_solution_length'], '\tMean length:', results['mean_solution_length'])

        try:
            with open(self.results_path, 'rb') as f:
                existing_results = pickle.load(f)
        except Exception as e:
            print(f'Starting new results log at {self.results_path} ({e})')
            existing_results = []

        existing_results.append(results)

        with open(self.results_path, 'wb') as f:
            pickle.dump(existing_results, f)

        torch.save(self.agent.q_function,
                   os.path.join(self.checkpoint_dir,
                                f'{self.n_checkpoints}.pt'))

        self.n_checkpoints += 1

        torch.save(self,
                   os.path.join(self.checkpoint_dir,
                                'training-state.pt'))


    def evaluate_agent(self):
        if self.n_checkpoints == 0:  # False when loading an existing training run.
            self.evaluate()
        while True:
            try:
                self.agent.learn_from_environment(self)
            except EndOfLearning:
                print('Learning budget ended. Doing last learning round (if agent wants to)')
                self.agent.learn_from_experience(self)
                print('Running final evaluation...')
                self.evaluate()
                break
            except Exception as e:
                traceback.print_exc(e)
                print('Ignoring exception and continuing...')

    def print_progress(self):
        print(util.now(), '{} steps ({:.3}%, ETA: {}), {} total reward, explored {} problems. {}'
              .format(self.n_steps,
                      100 * (self.n_steps / self.max_steps) if self.max_steps is not None
                          else 100 * (self.agent.training_problems_solved / len(self.agent.example_solutions)),
                      util.format_eta(datetime.datetime.now() - self.begin_time,
                                      self.n_steps if self.max_steps is not None else self.agent.training_problems_solved,
                                      self.max_steps if self.max_steps is not None else len(self.agent.example_solutions)),
                      self.cumulative_reward,
                      self.n_new_problems,
                      self.agent.stats()))


def evaluate_policy(config, device, verbose):
    if config.get('random_policy'):
        q = RandomQFunction()
    elif config.get('inverse_length'):
        q = InverseLength()
    else:
        q = torch.load(config['model_path'], map_location=device)

    q.to(device)
    q.device = device

    env = Environment.from_config(config)
    evaluator = SuccessRatePolicyEvaluator(env, config.get('eval_config', {}))
    result = evaluator.evaluate(q, verbose=verbose, show_progress=True)

    if verbose:
        print('Success rate:', result['success_rate'])
        print('Max solution length:', result['max_solution_length'])
        print('Solved problems:', result['successes'])
        print('Unsolved problems:', result['failures'])

    return result['success_rate']


def evaluate_policy_checkpoints(config, device):
    previous_successes = set()
    checkpoint_path = config['checkpoint_path']
    env = Environment.from_config(config)
    evaluator = SuccessRatePolicyEvaluator(env, config.get('eval_config', {}))
    i = 0
    last_hash = None

    try:
        while True:
            path = checkpoint_path.format(i)
            i += 1

            with open(path, 'rb') as f:
                h = hashlib.md5(f.read()).hexdigest()
                if h == last_hash:
                    continue
                last_hash = h
            print('Evaluating', path)
            q = torch.load(path, map_location=device)
            q.to(device)
            q.device = device
            result = evaluator.evaluate(q, show_progress=True)

            for j, p in result['successes']:
                if j not in previous_successes:
                    print(f'New success: {j} :: {p.facts[-1]} (length: {result["solution_lengths"][j]})')

            for j, p in result['failures']:
                if j in previous_successes or i == 1:
                    print('New failure:', j, '::', p.facts[-1])

            previous_successes = set([j for j, _ in result['successes']])

            print('Success rate:', result['success_rate'])

    except FileNotFoundError:
        print('Checkpoint', i, 'does not exist -- stopping.')


def normalize_solutions(solutions: list[list[str]]) -> list[list[str]]:
    'Uses the Racket parser to syntactically normalize solutions in the equations domain.'
    all_steps = []

    for s in solutions:
        all_steps.extend(s)

    with open('input.txt', 'w') as f:
        for l in all_steps:
            f.write(l)
            f.write('\n')

    sp = subprocess.run(["racket", "-tm", "canonicalize-terms.rkt"], capture_output=True)
    steps = list(filter(None, sp.stdout.decode("utf8").split("\n")))

    new_solutions = []
    for s in solutions:
        new_solutions.append([steps.pop(0) for _ in range(len(s))])

    return new_solutions


def normalize_human_solutions(path):
    human_solutions = json.load(open(path))

    solutions = []

    for h in human_solutions:
        solutions.extend(h['solutions'])

    normalized_solutions = normalize_solutions(solutions)

    for h in human_solutions:
        for i in range(len(h['solutions'])):
            h['solutions'][i] = normalized_solutions.pop(0)

    with open('normalized_human_solutions.json', 'w') as f:
        json.dump(human_solutions, f)
