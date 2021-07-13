from habitat.config.default import get_config
from habitat.core.benchmark import Benchmark
from habitat.tasks.rearrange.rearrange_sensors import RearrangePickSuccess
from habitat_baselines.agents.mp_agents import (
    AgentComposition,
    SpaManipPick,
    SpaResetModule,
)
from habitat_baselines.motion_planning.motion_plan import is_ompl_installed

TEST_CFG = "habitat_baselines/config/rearrange/spap_rearrangepick.yaml"


def test_pick_motion_planning():
    # This test will only run if OMPL is installed.
    if not is_ompl_installed():
        print("OMPL not installed skipping test")
        return
    config = get_config(TEST_CFG)

    benchmark = Benchmark(config.BASE_TASK_CONFIG_PATH)

    def get_args(skill):
        target_idx = skill._sim.get_targets()[0][0]
        return {"obj": target_idx}

    ac_cfg = get_config(config.BASE_TASK_CONFIG_PATH).TASK.ACTIONS
    spa_cfg = config.SPA
    env = benchmark._env
    pick_skill = AgentComposition(
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
    metrics = benchmark.evaluate(pick_skill, 1)
    assert metrics[RearrangePickSuccess.cls_uuid] == 1.0