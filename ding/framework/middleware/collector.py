from distutils.log import info
from easydict import EasyDict
from ding.policy import Policy, get_random_policy
from ding.envs import BaseEnvManager
from ding.framework import task, EventEnum
from .functional import inferencer, rolloutor, TransitionList, battle_inferencer, battle_rolloutor
from .actor_data import ActorData

# if TYPE_CHECKING:
from ding.framework import OnlineRLContext, BattleContext

from ding.worker.collector.base_serial_collector import CachePool

class BattleCollector:

    def __init__(self, cfg: EasyDict, env: BaseEnvManager, n_rollout_samples: int):
        self.cfg = cfg
        self.end_flag = False
        # self._reset(env)
        self.env = env
        self.env_num = self.env.env_num

        self.obs_pool = CachePool('obs', self.env_num, deepcopy=self.cfg.deepcopy_obs)
        self.policy_output_pool = CachePool('policy_output', self.env_num)

        self.total_envstep_count = 0
        self.end_flag = False
        self.n_rollout_samples = n_rollout_samples
        self.streaming_sampling_flag = n_rollout_samples > 0

        self._battle_inferencer = task.wrap(
            battle_inferencer(self.cfg, self.env, self.obs_pool, self.policy_output_pool)
        )
        self._battle_rolloutor = task.wrap(battle_rolloutor(self.cfg, self.env, self.obs_pool, self.policy_output_pool))

    def __del__(self) -> None:
        """
        Overview:
            Execute the close command and close the collector. __del__ is automatically called to \
                destroy the collector instance when the collector finishes its work
        """
        if self.end_flag:
            return
        self.end_flag = True
        self.env.close()

    def __call__(self, ctx: "BattleContext") -> None:
        """
        Input of ctx:
            - n_episode (:obj:`int`): the number of collecting data episode
            - train_iter (:obj:`int`): the number of training iteration
            - policy_kwargs (:obj:`dict`): the keyword args for policy forward
        Output of ctx:
            -  ctx.train_data (:obj:`Tuple[List, List]`): A tuple with training sample(data) and episode info, \
                the former is a list containing collected episodes if not get_train_sample, \
                otherwise, return train_samples split by unroll_len.
        """
        ctx.envstep = self.total_envstep_count
        if ctx.n_episode is None:
            if ctx._default_n_episode is None:
                raise RuntimeError("Please specify collect n_episode")
            else:
                ctx.n_episode = ctx._default_n_episode
        assert ctx.n_episode >= self.env_num, "Please make sure n_episode >= env_num"

        if ctx.policy_kwargs is None:
            ctx.policy_kwargs = {}

        if self.env.closed:
            self.env.launch()

        ctx.collected_episode = 0
        ctx.train_data = [[] for _ in range(ctx.agent_num)]
        ctx.episode_info = [[] for _ in range(ctx.agent_num)]
        ctx.ready_env_id = set()
        ctx.remain_episode = ctx.n_episode
        while True:
            self._battle_inferencer(ctx)
            self._battle_rolloutor(ctx)
            self.total_envstep_count = ctx.envstep

            if not ctx.job.is_eval and self.streaming_sampling_flag is True and len(ctx.train_data[0]) >= self.n_rollout_samples:
                actor_data = ActorData(env_step=ctx.envstep, train_data=ctx.train_data[0])
                task.emit(EventEnum.ACTOR_SEND_DATA.format(player=ctx.job.launch_player), actor_data)
                ctx.train_data = [[] for _ in range(ctx.agent_num)]

            if ctx.collected_episode >= ctx.n_episode:
                ctx.job.result = [e['result'] for e in ctx.episode_info[0]]
                task.emit(EventEnum.ACTOR_FINISH_JOB, ctx.job)
                if not ctx.job.is_eval and len(ctx.train_data[0]) > 0:
                    actor_data = ActorData(env_step=ctx.envstep, train_data=ctx.train_data[0])
                    task.emit(EventEnum.ACTOR_SEND_DATA.format(player=ctx.job.launch_player), actor_data)
                    ctx.train_data = [[] for _ in range(ctx.agent_num)]
                break




class StepCollector:
    """
    Overview:
        The class of the collector running by steps, including model inference and transition \
            process. Use the `__call__` method to execute the whole collection process.
    """

    def __init__(self, cfg: EasyDict, policy, env: BaseEnvManager, random_collect_size: int = 0) -> None:
        """
        Arguments:
            - cfg (:obj:`EasyDict`): Config.
            - policy (:obj:`Policy`): The policy to be collected.
            - env (:obj:`BaseEnvManager`): The env for the collection, the BaseEnvManager object or \
                its derivatives are supported.
            - random_collect_size (:obj:`int`): The count of samples that will be collected randomly, \
                typically used in initial runs.
        """
        self.cfg = cfg
        self.env = env
        self.policy = policy
        self.random_collect_size = random_collect_size
        self._transitions = TransitionList(self.env.env_num)
        self._inferencer = task.wrap(inferencer(cfg, policy, env))
        self._rolloutor = task.wrap(rolloutor(cfg, policy, env, self._transitions))

    def __call__(self, ctx: "OnlineRLContext") -> None:
        """
        Overview:
            An encapsulation of inference and rollout middleware. Stop when completing \
                the target number of steps.
        Input of ctx:
            - env_step (:obj:`int`): The env steps which will increase during collection.
        """
        old = ctx.env_step
        if self.random_collect_size > 0 and old < self.random_collect_size:
            target_size = self.random_collect_size - old
            random_policy = get_random_policy(self.cfg, self.policy, self.env)
            current_inferencer = task.wrap(inferencer(self.cfg, random_policy, self.env))
        else:
            # compatible with old config, a train sample = unroll_len step
            target_size = self.cfg.policy.collect.n_sample * self.cfg.policy.collect.unroll_len
            current_inferencer = self._inferencer

        while True:
            current_inferencer(ctx)
            self._rolloutor(ctx)
            if ctx.env_step - old >= target_size:
                ctx.trajectories, ctx.trajectory_end_idx = self._transitions.to_trajectories()
                self._transitions.clear()
                break


class EpisodeCollector:
    """
    Overview:
        The class of the collector running by episodes, including model inference and transition \
            process. Use the `__call__` method to execute the whole collection process.
    """

    def __init__(self, cfg: EasyDict, policy, env: BaseEnvManager, random_collect_size: int = 0) -> None:
        """
        Arguments:
            - cfg (:obj:`EasyDict`): Config.
            - policy (:obj:`Policy`): The policy to be collected.
            - env (:obj:`BaseEnvManager`): The env for the collection, the BaseEnvManager object or \
                its derivatives are supported.
            - random_collect_size (:obj:`int`): The count of samples that will be collected randomly, \
                typically used in initial runs.
        """
        self.cfg = cfg
        self.env = env
        self.policy = policy
        self.random_collect_size = random_collect_size
        self._transitions = TransitionList(self.env.env_num)
        self._inferencer = task.wrap(inferencer(cfg, policy, env))
        self._rolloutor = task.wrap(rolloutor(cfg, policy, env, self._transitions))

    def __call__(self, ctx: "OnlineRLContext") -> None:
        """
        Overview:
            An encapsulation of inference and rollout middleware. Stop when completing the \
                target number of episodes.
        Input of ctx:
            - env_episode (:obj:`int`): The env env_episode which will increase during collection.
        """
        old = ctx.env_episode
        if self.random_collect_size > 0 and old < self.random_collect_size:
            target_size = self.random_collect_size - old
            random_policy = get_random_policy(self.cfg, self.policy, self.env)
            current_inferencer = task.wrap(inferencer(self.cfg, random_policy, self.env))
        else:
            target_size = self.cfg.policy.collect.n_episode
            current_inferencer = self._inferencer

        while True:
            current_inferencer(ctx)
            self._rolloutor(ctx)
            if ctx.env_episode - old >= target_size:
                ctx.episodes = self._transitions.to_episodes()
                self._transitions.clear()
                break


# TODO battle collector