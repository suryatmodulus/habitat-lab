import abc
import argparse
from typing import Any, Dict, List

import numpy as np

import habitat
from habitat.config.default import get_config
from habitat.core.simulator import Observations
from habitat.tasks.rearrange.actions import get_empty_action
from habitat_baselines.agents.benchmark_render import BenchmarkRenderer
from habitat_baselines.motion_planning.motion_plan import MotionPlanner
from habitat_baselines.motion_planning.robot_target import RobotTarget


class ParameterizedAgent(habitat.Agent):
    def __init__(
        self,
        env,
        config,
        action_config,
        should_auto_end=True,
        auto_get_args_fn=None,
    ):
        self._should_auto_end = should_auto_end
        self._auto_get_args_fn = auto_get_args_fn
        self._last_info = {}

        self._config = config
        self._agent_config = action_config

        self._sim = env._sim
        self._task = env

    def _end_episode(self):
        self._task.end_episode()

    def _set_info(self, k: str, v: Any) -> None:
        self._last_info[k] = v

    def _has_info(self, k: str) -> bool:
        return k in self._last_info

    def get_and_clear_info(self) -> Dict[str, Any]:
        ret = self._last_info
        self._last_info = {}
        return ret

    def reset(self) -> None:
        if self._auto_get_args_fn is not None:
            self.set_args(**self._auto_get_args_fn(self))

    def set_args(self, **kwargs) -> None:
        pass

    def _log(self, txt):
        if self._config.VERBOSE:
            print("%s: %s" % (str(self), txt))

    def act(self, observations: Observations) -> Dict[str, Any]:
        if self._should_auto_end:
            self._end_episode()
        return {}

    def should_term(self, observations: Observations) -> bool:
        return False


class AgentComposition(ParameterizedAgent):
    def __init__(
        self,
        skills,
        env,
        config,
        action_config,
        should_auto_end=True,
        auto_get_args_fn=None,
    ):
        super().__init__(
            env, config, action_config, should_auto_end, auto_get_args_fn
        )
        self.skills: List[ParameterizedAgent] = skills
        self.cur_skill: int = 0

    def _set_info(self, k, v):
        self._last_info[k] = v

    def _has_info(self, k):
        return any([skill._has_info(k) for skill in self.skills])

    def get_and_clear_info(self):
        r = {}
        for skill in self.skills:
            r.update(skill.get_and_clear_info())
        return r

    def set_args(self, **kwargs):
        self._enter_kwargs = kwargs
        self.skills[self.cur_skill].set_args(**self._enter_kwargs)

    def reset(self):
        super().reset()
        self.cur_skill = 0
        self.skills[self.cur_skill].reset()

    def act(self, observations):
        if self.should_term(observations):
            return get_empty_action()

        action = self.skills[self.cur_skill].act(observations)
        return action

    def should_term(self, observations):
        if self.cur_skill >= len(self.skills):
            return True
        if self.skills[self.cur_skill].should_term(observations):
            self.cur_skill += 1
            if self.cur_skill < len(self.skills):
                self._log(f"Moving to skill {self.skills[self.cur_skill]}")
                self.skills[self.cur_skill].reset()
                self.skills[self.cur_skill].set_args(**self._enter_kwargs)
        return self.cur_skill >= len(self.skills)


class ArmTargModule(ParameterizedAgent):
    """Reaches the arm to a target position."""

    def __init__(
        self,
        env,
        config,
        action_config,
        should_auto_end=True,
        auto_get_args_fn=None,
    ):
        super().__init__(
            env, config, action_config, should_auto_end, auto_get_args_fn
        )
        self._grasp_thresh = self._agent_config.ARM_ACTION.GRASP_THRESH_DIST
        self._viz_points = []

        self._mp = MotionPlanner(self._sim, self._config)
        self._mp.set_should_render(self._config.MP_RENDER)
        self._enter_kwargs = None

    @property
    def wait_after(self) -> int:
        return 0

    @property
    def timeout(self) -> int:
        return 400

    def set_args(self, **kwargs) -> None:
        self._log(f"Set arm targ args {kwargs}")
        self._enter_kwargs = kwargs

    def reset(self) -> None:
        self._enter_kwargs = None
        super().reset()
        self._log("Entered arm targ")
        self._plan_idx = 0
        self._term = False
        self._clean_viz_points()

        self._viz_points = []
        self._plan: Any = None
        self._has_generated_plan = False

    def _add_debug_viz_point(self, pos):
        pos_name = f"arm_targ_{len(self._viz_points)}"
        self._sim.viz_ids[pos_name] = self._sim.viz_pos(
            pos, self._sim.viz_ids[pos_name]
        )
        self._viz_points.append(pos_name)

    def act(self, observations: Observations) -> Dict[str, Any]:
        assert self._enter_kwargs is not None, "Need to first call `set_args`!"

        if not self._has_generated_plan:
            self._plan = self._generate_plan(
                observations, **self._enter_kwargs
            )
            self._has_generated_plan = True

        cur_plan_ac = self._get_plan_ac(observations)
        if cur_plan_ac is None:
            self._term = True
            return get_empty_action()

        self._plan_idx += 1
        grip = self._get_gripper_ac(cur_plan_ac)
        if not self._is_ee_plan:
            des_js = cur_plan_ac
            return {
                "action": "ARM_ACTION",
                "action_args": {"arm_action": des_js, "grip_action": grip},
            }
        else:
            raise NotImplementedError("EE control not yet supported")

    def _get_plan_ac(self, observations) -> np.ndarray:
        r"""Get the plan action for the current timestep. By default return the
        action at the current plan index.
        """
        if self._plan is None:
            self._log("Planning failed")
            self._end_episode()
            return None
        if self.adjusted_plan_idx >= len(self._plan):
            return self._plan[-1]
        else:
            return self._plan[self.adjusted_plan_idx]

    def _internal_should_term(self, observations):
        return False

    def should_term(self, observations: Observations) -> bool:
        done = self._term
        if (
            self._plan is not None
            and self.adjusted_plan_idx >= len(self._plan) + self.wait_after
        ):
            self._log("Plan finished")
            done = True

        if self._plan_idx > self.timeout:
            self._log("Skill timed out")
            done = True

        if self._has_generated_plan and self._internal_should_term(
            observations
        ):
            self._log("Skill requested termination")
            done = True

        if done:
            self._on_done()
        return done

    def _get_force_set_ee(self):
        return None

    def _on_done(self):
        self._clean_viz_points()

    def _clean_viz_points(self):
        if not self._config.VERBOSE:
            return
        for viz_point_name in self._viz_points:
            if self._sim.viz_ids[viz_point_name] is None:
                continue
            self._sim.remove_object(self._sim.viz_ids[viz_point_name])
            del self._sim.viz_ids[viz_point_name]
        self._viz_points = []

    @abc.abstractmethod
    def _get_gripper_ac(self, plan_ac) -> float:
        pass

    @property
    def adjusted_plan_idx(self) -> bool:
        return self._plan_idx // self._config.RUN_FREQ

    @property
    def _is_ee_plan(self) -> bool:
        return False

    @abc.abstractmethod
    def _generate_plan(self, observations, **kwargs) -> np.ndarray:
        r"""Gets the plan this controller will execute.

        :return: Either a sequence of 3D EE targets or a sequence of arm joint
            targets.
        """

    def _clean_mp(self):
        if self._mp.traj_viz_id is not None:
            self._sim._sim.remove_traj_obj(self._mp.traj_viz_id)
            self._mp.traj_viz_id = None


class SpaManipPick(ArmTargModule):
    @property
    def wait_after(self):
        return 5

    def _internal_should_term(self, observations):
        is_holding = observations["is_holding"].item() == 1
        if is_holding:
            self._log("Robot is holding object, leaving pick")
            # Override indicating we succeeded
            self._set_info("execute_pick_failure", 0)
            return True
        else:
            return False

    def _generate_plan(self, observations, obj, **kwargs):
        self._set_info("execute_ee_to_obj_dist", 0)
        self._set_info("execute_ee_dist", 0)

        self._mp.set_config(
            self._config.MP_MARGIN,
            self._config.MP_OBJ,
            self._grasp_thresh,
            self._config.N_GRASPS,
            self._config,
        )
        obj_idx = self._sim.scene_obj_ids[obj]
        robo_targ = self._mp.grasp_gen.gen_target_from_obj_idx(obj_idx)
        self._targ_obj_idx = obj_idx
        self._robo_targ = robo_targ

        if self._config.VERBOSE:
            self._add_debug_viz_point(robo_targ.ee_targ)

        plan = self._mp.motion_plan(
            self._sim.robot.arm_joint_pos,
            robo_targ,
            observations,
            timeout=self._config.TIMEOUT,
        )

        for k, v in self._mp.get_recent_plan_stats(plan, robo_targ).items():
            self._set_info(k, v)
        self._set_info("execute_bad_coll_failure", int(self._mp.was_bad_coll))
        # Don't double count execute failure.
        self._set_info("execute_failure", int(plan is not None))
        return plan

    def _on_done(self):
        super()._on_done()
        cur_ee = self._sim.robot.ee_transform.translation
        obj_pos = np.array(self._sim.get_translation(self._targ_obj_idx))

        ee_dist = np.linalg.norm(self._robo_targ.ee_targ - cur_ee)
        ee_dist_to_obj = np.linalg.norm(obj_pos - cur_ee)
        if (
            ee_dist_to_obj < self._grasp_thresh
            and ee_dist < self._config.EXEC_EE_THRESH
        ):
            self._set_info("execute_failure", 0)
            self._set_info("execute_bad_coll_failure", 0)
        else:
            self._set_info("execute_ee_to_obj_dist", ee_dist_to_obj)
            self._set_info("execute_ee_dist", ee_dist)
        self._clean_mp()

    def _get_gripper_ac(self, plan_ac):
        if self.adjusted_plan_idx >= len(self._plan):
            grip = 1
        else:
            grip = -1
        return grip


class SpaResetModule(ArmTargModule):
    def __init__(
        self,
        env,
        config,
        action_config,
        should_auto_end=True,
        ignore_first=False,
        auto_get_args_fn=None,
    ):
        super().__init__(
            env, config, action_config, should_auto_end, auto_get_args_fn
        )
        self._ignore_first = ignore_first

    def _generate_plan(self, observations, **kwargs):
        self._mp.set_config(
            self._config.MP_MARGIN,
            self._config.MP_OBJ,
            self._grasp_thresh,
            self._config.N_GRASPS,
            self._config,
            ignore_first=self._ignore_first,
            use_prev=True,
        )

        robo_targ = RobotTarget(js_targ=self._mp.grasp_gen.get_def_js())
        plan = self._mp.motion_plan(
            self._sim.robot.arm_joint_pos,
            robo_targ,
            observations,
            timeout=self._config.TIMEOUT,
        )

        for k, v in self._mp.get_recent_plan_stats(
            plan, robo_targ, "reset_"
        ).items():
            self._set_info(k, v)
        self._set_info(
            "execute_reset_bad_coll_failure", int(self._mp.was_bad_coll)
        )
        # Don't double count execute failure.
        self._set_info("execute_reset_failure", int(plan is not None))
        return plan

    @property
    def _is_ee_plan(self):
        return False

    def _on_done(self):
        super()._on_done()
        self._set_info("execute_reset_failure", 0)
        self._set_info("execute_reset_bad_coll_failure", 0)
        self._clean_mp()

    def _get_gripper_ac(self, plan_ac):
        if self._sim.robot.is_gripper_open:
            grip = -1.0
        else:
            grip = 1.0
        return grip

    @property
    def wait_after(self):
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-type", default="pick")
    parser.add_argument("--num-eval", type=int, default=None)
    parser.add_argument("--traj-save-path", type=str, default=None)
    parser.add_argument(
        "opts",
        default=None,
        nargs=argparse.REMAINDER,
        help="Modify config options from command line",
    )
    args = parser.parse_args()
    cfg_path = "habitat_baselines/config/rearrange/spap_rearrangepick.yaml"

    config = get_config(cfg_path, args.opts)
    benchmark = BenchmarkRenderer(
        config.BASE_TASK_CONFIG_PATH,
        config.VIDEO_OPTIONS,
        config.VIDEO_DIR,
        {
            "rearrangepick_success",
        },
        args.traj_save_path,
    )

    ac_cfg = get_config(config.BASE_TASK_CONFIG_PATH).TASK.ACTIONS
    spa_cfg = config.SPA
    env = benchmark._env

    def get_args(skill):
        target_idx = skill._sim.get_targets()[0][0]
        return {"obj": target_idx}

    skills = {
        "pick": AgentComposition(
            [
                SpaManipPick(env, spa_cfg, ac_cfg, auto_get_args_fn=get_args),
                SpaResetModule(
                    env,
                    spa_cfg,
                    ac_cfg,
                    ignore_first=True,
                    auto_get_args_fn=get_args,
                ),
            ],
            env,
            spa_cfg,
            ac_cfg,
            auto_get_args_fn=get_args,
        )
    }
    use_skill = skills[args.skill_type]

    metrics = benchmark.evaluate(use_skill, args.num_eval)
    for k, v in metrics.items():
        habitat.logger.info("{}: {:.3f}".format(k, v))


if __name__ == "__main__":
    main()