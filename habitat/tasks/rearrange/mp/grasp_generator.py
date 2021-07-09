import magnum as mn
import numpy as np
from PIL import Image

from habitat.tasks.rearrange.mp.robot_target import (
    ObjPlanningData,
    RobotTarget,
)

VERBOSE = False


class GraspGenerator:
    def __init__(
        self,
        use_sim,
        mp_space,
        ik,
        mp,
        should_render,
        grasp_thresh,
        n_gen_grasps,
        knows_other_objs,
        log_dir,
    ):
        self._mp_sim = use_sim
        self._ik = ik
        self._mp_space = mp_space
        (
            self._lower_joint_lims,
            self._upper_joint_lims,
        ) = self._ik.get_joint_limits()
        self.mp = mp
        self._should_render = should_render
        self._grasp_thresh = grasp_thresh
        self._n_gen_grasps = n_gen_grasps
        self.knows_other_objs = knows_other_objs
        self._log_dir = log_dir

    def get_def_js(self):
        # A reference state which we should generally stay close to.
        return np.array([-0.45, -1.08, 0.1, 0.935, -0.001, 1.573, 0.005])

    def get_targ_obj(self, start_js, obj_id):
        pass

    def _gen_goal_state(self, local_ee_targ, grasp_idx=0, timeout=100):
        """
        - local_ee_targ: 3D desired EE position in robot's base coordinate frame.
        - grasp_idx: The grasp index attempt. Used for debugging.
        Returns: (target_js, is_feasible) target_js has joint position to
        achieve EE target. is_feasible is if a collision JS was found.
        """

        start_state = self._mp_sim.capture_state()

        self._mp_space.set_env_state(start_state)
        start_js = self._mp_sim.get_arm_pos()

        start_arm_js = self._mp_sim.get_arm_pos()
        state_lims = self._mp_space.get_state_lims(True)
        lower_lims = state_lims[:, 0]
        upper_lims = state_lims[:, 1]
        found_sol = None
        for iter_i in range(timeout):
            if iter_i == 0:
                # Check if the starting state can already achieve the goal.
                cur_js = np.array(start_arm_js)
            else:
                cur_js = np.random.uniform(lower_lims, upper_lims)

            self._ik.set_arm_state(cur_js, np.zeros(cur_js.shape))
            desired_js = self._ik.calc_ik(local_ee_targ)
            state_valid = all(
                [self._is_state_valid_fn(desired_js) for _ in range(5)]
            )
            if state_valid:
                found_sol = np.array(desired_js)
                break

        self._mp_sim.set_arm_pos(start_js)
        self._mp_sim.set_state(start_state)
        return found_sol, found_sol is not None

    def _fk(self, js):
        self._mp_sim.set_arm_pos(js)
        self._mp_sim.micro_step()

    def gen_target_from_ee_pos(self, ee_pos):
        inv_robo_T = self._mp_sim.get_robot_transform().inverted()
        local_ee_pos = inv_robo_T.transform_point(ee_pos)

        self.mp.setup_ee_margin(None)
        self._is_state_valid_fn = self.mp._is_state_valid

        use_js = None
        real_ee_pos = None
        for _ in range(20):
            js, is_feasible = self._gen_goal_state(local_ee_pos)
            if not is_feasible:
                continue
            real_ee_pos = self._get_real_ee_pos(js)
            ee_dist = np.linalg.norm(real_ee_pos - ee_pos)
            if ee_dist < self._grasp_thresh:
                use_js = js
                break

        targ = RobotTarget(
            js_targ=use_js, is_guess=use_js is None, ee_targ=real_ee_pos
        )

        self.mp.remove_ee_margin(None)
        return targ

    def _verbose_log(self, s):
        if VERBOSE:
            print(f"GraspPlanner: {s}")

    def get_obj_goal_offset(self, obj_idx):
        obj_dat = self._mp_sim.get_obj_info(obj_idx)
        size_y = obj_dat.bb.size_y() / 2.0
        return np.array([0.0, size_y, 0.0])

    def _bounding_box_sample(
        self, obj_idx: int, obj_dat: ObjPlanningData
    ) -> RobotTarget:
        """
        DEPRECATED use _bounding_sphere_sample instead.

        Return target joint and end-effector position based on the object
        bounding box.
        """
        T = obj_dat.trans
        offset_dist = 0.03
        size_y = obj_dat.bb.size_y() / 2.0
        size_z = obj_dat.bb.size_z() / 2.0
        size_x = obj_dat.bb.size_x() / 2.0

        # Centers of bounding box faces
        bb_points = [
            (
                T.transform_point(mn.Vector3(0.0, offset_dist + size_y, 0.0)),
                size_y,
            ),
            (
                T.transform_point(mn.Vector3(0.0, 0.0, offset_dist + size_z)),
                size_z,
            ),
            (
                T.transform_point(
                    mn.Vector3(0.0, 0.0, -(offset_dist + size_z))
                ),
                size_z,
            ),
            (
                T.transform_point(mn.Vector3(offset_dist + size_x, 0.0, 0.0)),
                size_x,
            ),
            (
                T.transform_point(
                    mn.Vector3(-(offset_dist + size_x), 0.0, 0.0)
                ),
                size_x,
            ),
        ]

        # Prefer the point on top of the object since it is typically easiest to grasp.
        bb_points = sorted(bb_points, key=lambda x: x[0][1], reverse=True)
        # Uncomment to visualize the candidate grasp positions on the bounding box.
        # if self._should_render:
        #    for bb_point in bb_points:
        #        #self._mp_sim.create_viz(bb_point[0])
        #        self._mp_sim._sim.viz_pos(bb_point[0], None)

        inv_robo_T = self._mp_sim.get_robot_transform().inverted()

        self.mp.setup_ee_margin(obj_idx)
        self._is_state_valid_fn = self.mp._is_state_valid

        # Try to generate a valid JS for the desired EE pos.
        for i, (bb_point, _) in enumerate(bb_points):
            local_bb_point = inv_robo_T.transform_point(bb_point)
            local_bb_point = np.array(local_bb_point)
            goal_js, is_feasible = self._gen_goal_state(local_bb_point)
            if is_feasible:
                if self._should_render:
                    print("Using grasp point idx", i)
                break
        self.mp.remove_ee_margin(obj_idx)

        if not is_feasible:
            print("Nothing was feasible")
            # If nothing works, first priority is first specified bb point.
            goal_js, _ = self._gen_goal_state(np.array(bb_points[0][0]))

        # Generate grasp positions on each side of the bounding box
        return RobotTarget(
            js_targ=goal_js,
            obj_targ=obj_idx,
            is_guess=not is_feasible,
            ee_targ=bb_point,
        )

    def _bounding_sphere_sample(
        self, obj_idx: int, obj_dat: ObjPlanningData
    ) -> RobotTarget:
        obj_pos = np.array(obj_dat.trans.translation)

        inv_robo_T = self._mp_sim.get_robot_transform().inverted()

        # Setup extra collision checkers
        self.mp.setup_ee_margin(obj_idx)
        self._is_state_valid_fn = self.mp._is_state_valid

        # Get the candidate grasp points in global space.
        min_radius = self._grasp_thresh * 0.5

        sim = self._mp_sim._sim
        scene_obj_ids = sim.scene_obj_ids
        scene_obj_pos = sim.get_scene_pos()

        found_goal_js = None
        real_ee_pos = None

        for i in range(self._n_gen_grasps):
            self._verbose_log(f"Trying for {i}")

            # Generate a grasp 3D point
            radius = np.random.uniform(min_radius, self._grasp_thresh)
            point = np.random.randn(3)
            point[1] = np.abs(point[1])
            point = radius * (point / np.linalg.norm(point))
            point += obj_pos

            if self.knows_other_objs:
                closest_idx = np.argmin(
                    np.linalg.norm(scene_obj_pos - point, axis=-1)
                )
                if scene_obj_ids[closest_idx] != obj_idx:
                    self._verbose_log(
                        "Grasp point didn't match desired object"
                    )
                    continue

            local_point = inv_robo_T.transform_point(point)
            local_point = np.array(local_point)

            self._grasp_debug_points(obj_pos, point)

            goal_js, is_feasible = self._gen_goal_state(local_point, i)
            if not is_feasible:
                self._verbose_log("Could not find JS for grasp point")
                continue

            # Check the final end-effector position is indeed within
            # grasping position of the object.
            real_ee_pos = self._get_real_ee_pos(goal_js)

            ee_dist = np.linalg.norm(real_ee_pos - obj_pos)
            if ee_dist >= self._grasp_thresh:
                found_goal_js = goal_js
                self._verbose_log("Actual EE wasn't in grasp range")
                continue

            if self.knows_other_objs:
                # Does the actual end-effector position grasp the object we want?
                closest_idx = np.argmin(
                    np.linalg.norm(scene_obj_pos - real_ee_pos, axis=-1)
                )
                if scene_obj_ids[closest_idx] != obj_idx:
                    self._verbose_log("Actual EE did not match desired object")
                    continue

            if self._should_render:
                sim.viz_ids["ee"] = sim.viz_pos(
                    real_ee_pos, sim.viz_ids["ee"], r=5.0
                )
                Image.fromarray(self._mp_sim.render()).save(
                    f"data/{self._log_dir}/grasp_plan_{i}_{ee_dist}.jpeg"
                )

            self._verbose_log(f"Found solution at {i}, breaking")
            found_goal_js = goal_js
            break

        self._clean_grasp_debug_points()
        self.mp.remove_ee_margin(obj_idx)

        return RobotTarget(
            js_targ=found_goal_js,
            obj_targ=obj_idx,
            is_guess=found_goal_js is None,
            ee_targ=real_ee_pos,
        )

    def _get_real_ee_pos(self, js):
        if js is None:
            return None
        start_state = self._mp_sim.capture_state()
        start_js = self._mp_sim.get_arm_pos()
        self._mp_sim.set_arm_pos(js)
        self._mp_sim.micro_step()
        real_ee_pos = self._mp_sim.get_ee_pos()
        self._mp_sim.set_arm_pos(start_js)
        self._mp_sim.set_state(start_state)
        return real_ee_pos

    def _clean_grasp_debug_points(self):
        sim = self._mp_sim._sim
        if self._should_render:
            # Cleanup any debug render objects.
            if sim.viz_ids["ee"] is not None:
                sim.remove_object(sim.viz_ids["ee"])
            if sim.viz_ids["obj"] is not None:
                sim.remove_object(sim.viz_ids["obj"])
                sim.remove_object(sim.viz_ids["grasp"])

            sim.viz_ids["obj"] = None
            sim.viz_ids["grasp"] = None
            sim.viz_ids["ee"] = None

    def _grasp_debug_points(self, obj_pos, grasp_point):
        sim = self._mp_sim._sim
        if self._should_render:
            sim.viz_ids["obj"] = sim.viz_pos(
                obj_pos, sim.viz_ids["obj"], r=5.0
            )

            sim.viz_ids["grasp"] = sim.viz_pos(
                grasp_point, sim.viz_ids["grasp"], r=5.0
            )

    def gen_target_from_obj_idx(self, obj_idx):
        obj_dat = self._mp_sim.get_obj_info(obj_idx)

        # return self._bounding_box_sample(obj_idx, obj_dat)
        return self._bounding_sphere_sample(obj_idx, obj_dat)