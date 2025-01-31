import multiprocessing
from ReinforcementLearning.rl.core import LoggerRL, TrajBatch
from ReinforcementLearning.utils.load_save import Memory
from ReinforcementLearning.utils.torch import *
import math
import time
import os
import platform
if platform.system() != "Linux":
    from multiprocessing import set_start_method
    set_start_method("fork")
os.environ["OMP_NUM_THREADS"] = "1"



class Agent:

    def __init__(self, env, policy_net, value_net, dtype, device, gamma,
                 num_threads=1, logger_cls=LoggerRL, logger_kwargs=None, traj_cls=TrajBatch):
        self.env = env
        self.policy_net = policy_net
        self.value_net = value_net
        self.dtype = dtype
        self.device = device
        self.gamma = gamma
        self.num_threads = num_threads
        self.noise_rate = 1.0
        self.traj_cls = traj_cls
        self.logger_cls = logger_cls
        self.logger_kwargs = dict() if logger_kwargs is None else logger_kwargs
        self.sample_modules = [policy_net]
        self.update_modules = [policy_net, value_net]

    def sample_worker(self, pid, queue, num_samples, mean_action):
        self.seed_worker(pid)
        memory = Memory()
        logger = self.logger_cls(**self.logger_kwargs)

        while logger.num_steps < num_samples:
            state = self.env.reset()

            last_info = dict()
            episode_success = False
            logger_messages = []
            memory_messages = []
            for t in range(10000):
                state_var = self.tensorfy([state])
                use_mean_action = mean_action or torch.bernoulli(torch.tensor([1 - self.noise_rate])).item()
                action = self.policy_net.select_action(state_var, use_mean_action).numpy().squeeze(0)
                next_state, reward, done, info = self.env.step(action, self.thread_loggers[pid])
                # cache logging
                logger_messages.append([reward, info])

                mask = 0 if done else 1
                exp = 1 - use_mean_action
                # cache memory
                memory_messages.append([state, action, mask, next_state, reward, exp])

                if done:
                    episode_success = (reward != self.env.FAILURE_REWARD) and (reward != self.env.INTERMEDIATE_REWARD)
                    last_info = info
                    break
                state = next_state

            if episode_success:
                logger.start_episode(self.env)
                for var in range(len(logger_messages)):
                    logger.step(self.env, *logger_messages[var])
                    self.push_memory(memory, *memory_messages[var])
                logger.end_episode(last_info)
                self.thread_loggers[pid].info('worker {} finished episode {}.'.format(pid, logger.num_episodes))

        if queue is not None:
            queue.put([pid, memory, logger])
        else:
            return memory, logger

    def seed_worker(self, pid):
        if pid > 0:
            torch.manual_seed(torch.randint(0, 5000, (1,)) * pid)
            np.random.seed(np.random.randint(5000) * pid)

    def push_memory(self, memory, state, action, mask, next_state, reward, exp):
        memory.push(state, action, mask, next_state, reward, exp)

    def sample(self, num_samples, mean_action=False, nthreads=None):
        if nthreads is None:
            nthreads = self.num_threads
        t_start = time.time()
        to_test(*self.sample_modules)
        with to_cpu(*self.sample_modules):
            with torch.no_grad():
                thread_num_samples = int(math.floor(num_samples / nthreads))
                queue = multiprocessing.Queue()
                memories = [None] * nthreads
                loggers = [None] * nthreads
                for i in range(nthreads-1):
                    worker_args = (i+1, queue, thread_num_samples, mean_action)
                    worker = multiprocessing.Process(target=self.sample_worker, args=worker_args)
                    worker.start()
                memories[0], loggers[0] = self.sample_worker(0, None, thread_num_samples, mean_action)

                for i in range(nthreads - 1):
                    pid, worker_memory, worker_logger = queue.get()
                    memories[pid] = worker_memory
                    loggers[pid] = worker_logger
                
                traj_batch = self.traj_cls(memories)

                logger = self.logger_cls.merge(loggers, **self.logger_kwargs)

        logger.sample_time = time.time() - t_start
        return traj_batch, logger

    def trans_policy(self, states):
        """transform states before going into policy net"""
        return states

    def trans_value(self, states):
        """transform states before going into value net"""
        return states

    def set_noise_rate(self, noise_rate):
        self.noise_rate = noise_rate


    def tensorfy(self,np_list, device=torch.device('cpu')):
        if isinstance(np_list[0], list):
            return [[torch.tensor(x).to(device) for x in y] for y in np_list]
        else:
            return [torch.tensor(y).to(device) for y in np_list]