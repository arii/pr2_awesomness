import numpy as np
import rospy
import roslib; 
roslib.load_manifest("pr2_awesomeness")
roslib.load_manifest("ee_cart_imped_action")
from pr2_common_action_msgs.msg import \
        ArmMoveIKAction, ArmMoveIKGoal, ArmMoveIKActionFeedback
from geometry_msgs.msg import Pose, Quaternion, Point
import actionlib
from pr2_controllers_msgs.msg import JointTrajectoryAction, \
        JointTrajectoryGoal,JointTrajectoryControllerState,\
        Pr2GripperCommandAction, Pr2GripperCommandGoal, Pr2GripperCommand
from trajectory_msgs.msg import JointTrajectoryPoint
import tf
from pr2_gripper_sensor_msgs.msg import  \
        PR2GripperEventDetectorAction, PR2GripperEventDetectorGoal
from sensor_msgs.msg import JointState
from object_manipulator.convert_functions import *
from gripper_controller import GripperController
from pr2_controller_manager import Pr2ControllerManager
import ee_cart_imped_action
from utils import check_cartesian_near_pose 


class tool_frame_ik:
    def __init__(self, tf_listener=None):
        if tf_listener == None:
            self.tf_listener = tf.TransformListener()
        else:
            self.tf_listener = tf_listener
        self.arms = ['r', 'l']
        self.mat = {}

        for arm in self.arms:
            wrist =arm+ "_wrist_roll_link"
            gripper =arm+ "_gripper_tool_frame"
            self.tf_listener.waitForTransform(gripper, wrist, rospy.Time(0), rospy.Duration(2))
            (trans, rot) = self.tf_listener.lookupTransform(\
            gripper, wrist, rospy.Time())

            mat = np.matrix(tf.transformations.quaternion_matrix(rot))
            pos = np.matrix(trans).T
            mat[0:3,3] = pos
            self.mat[arm] = mat
    def transform_pt(self, whicharm, trans, rot):
        mat = self.mat[whicharm]
        pose_mat = np.matrix(tf.transformations.quaternion_matrix(rot))
        pose_mat[0:3,3] = np.matrix(trans).T
        new_pose = pose_mat * mat
        return mat_to_pos_and_quat(new_pose)

class cartesian_pose_listener:
    def __init__(self, tf_listener=None):
        if tf_listener == None:
            self.tf_listener = tf.TransformListener()
        else:
            self.tf_listener = tf_listener
  
    #return the current Cartesian pose of the gripper
    def return_cartesian_pose(self, whicharm, frame = 'base_link'):
        start_time = rospy.get_rostime()
        current_time = rospy.get_rostime()
        while current_time - start_time < rospy.Duration(30.):
            current_time = rospy.get_rostime()
            try:
                t = self.tf_listener.getLatestCommonTime(\
                        frame, whicharm+'_wrist_roll_link')
                (trans, rot) = self.tf_listener.lookupTransform(\
                        frame, whicharm+'_wrist_roll_link', t)
                return list(trans) + list(rot)
            except (tf.Exception, tf.ExtrapolationException):
                rospy.sleep(0.5)
                current_time = rospy.get_rostime()
                rospy.logerr(\
                "waiting for a tf transform between %s and %s"%\
                (frame, whicharm+'_wrist_roll_link'))
        rospy.logerr("return_cartesian_pose waited 10 seconds tf\
                transform!  Returning None")
        return None

class joint_listener:
    def __init__(self):
        self.arms = ['r','l']
        self.joint_positions = {}
        self.joint_velocities = {}
        self.joint_names = [
                "_shoulder_pan_joint",
                "_shoulder_lift_joint",
                "_upper_arm_roll_joint",
                "_elbow_flex_joint",
                "_forearm_roll_joint",
                "_wrist_flex_joint",
                "_wrist_roll_joint"]
        self.sub = rospy.Subscriber("joint_states", JointState, self.callback)
    
    def callback(self, data):
        positions = dict(zip(data.name, data.position))
        velocities = dict(zip(data.name, data.velocity))
        for arm in self.arms:
            arm_pos = [ positions[arm + name ] for name in self.joint_names]
            arm_vel = [ velocities[arm + name ] for name in self.joint_names]
            self.joint_positions[arm] = arm_pos
            self.joint_velocities[arm] = arm_vel

    def get_joint_angles(self):
        return self.joint_positions
    def get_joint_velocities(self):
        return self.joint_velocities


class ArmController:
    def __init__(self, event_detector=False, tf_listener=None):
        if tf_listener == None:
            self.tf_listener = tf.TransformListener()
        else:
            self.tf_listener = tf_listener

        self.arms = ['r', 'l']
        self.cart_client = {}
        self.joint_client = {}
        self.controllers = {}
        self.using_joint_controller = {}
        self.jl = joint_listener()
        self.cartl = cartesian_pose_listener(tf_listener)
        self.gripper = GripperController(event_detector)
        self.tool_ik = tool_frame_ik(tf_listener)
        self.control= {
                'l': ee_cart_imped_action.EECartImpedClient("left_arm"),
                'r': ee_cart_imped_action.EECartImpedClient("right_arm")
                }


        self.pr2_controller_manager = Pr2ControllerManager()
        for arm in self.arms:
            self.pr2_controller_manager.start_controller(arm, "joint")
            self.using_joint_controller[arm] = False
        
        
        controller_names= [
                "_shoulder_pan_joint",
                "_shoulder_lift_joint",
                "_upper_arm_roll_joint",
                "_elbow_flex_joint",
                "_forearm_roll_joint",
                "_wrist_flex_joint",
                "_wrist_roll_joint"]

        for arm in self.arms:
            self.cart_client[arm] = actionlib.SimpleActionClient(\
                    arm+"_arm_ik", ArmMoveIKAction)
            self.joint_client[arm] = actionlib.SimpleActionClient(\
                    arm+"_arm_controller/joint_trajectory_action", \
                    JointTrajectoryAction)
            self.controllers[arm] = [arm +name for name in controller_names]
        rospy.sleep(1)
    
    ## Gripper functions
    def open_gripper(self, whicharm):
        self.gripper.open(whicharm)

    def close_gripper(self, whicharm):
        self.gripper.close(whicharm)

    ## Current Arm State Functions
    def get_cartesian_pose(self, frame_id="base_link"):
        poses = {}
        for arm in self.arms:
            poses[arm] = self.cartl.return_cartesian_pose(arm, frame_id)
        return poses

    def get_joint_angles(self):
        current_joint_angles = self.jl.get_joint_angles()
        return current_joint_angles
         
    def is_moving(self, whicharm):
        velocities = self.jl.get_joint_velocities()[whicharm]
        abs_v = [abs(v) for v in velocities]
        total_v = sum(abs_v)
        return total_v > .001

    ## Joint movement commands
    def joint_movearm(self, whicharm, joint_angles, \
            move_duration=5, blocking=True):
        if not self.using_joint_controller[whicharm]:
            self.pr2_controller_manager.start_controller(whicharm, "joint")
            self.using_joint_controller[whicharm] = True
        goal = JointTrajectoryGoal()
        goal.trajectory.joint_names = self.controllers[whicharm]
        point = JointTrajectoryPoint()
        point.positions = joint_angles
        point.time_from_start=rospy.Duration(move_duration)
        goal.trajectory.points.append(point)
        self.joint_client[whicharm].send_goal(goal)
        if blocking:
            self.joint_client[whicharm].wait_for_result(rospy.Duration(move_duration))
            return self.joint_client[whicharm].get_state() >= 3

    def joint_movearms(self, poses, move_duration=5, blocking=True):
        for arm in self.arms:
            self.joint_movearm(arm, poses[arm], move_duration, False)
        if blocking:
            result = True
            for arm in self.arms:
                self.joint_client[arm].wait_for_result()
                result  &= self.joint_client[arm].get_state()>=3
            return result
    
    def cart_freeze_arm(self, whicharm):
        curr = self.get_cartesian_pose()
        self.cart_movearm(whicharm, curr[whicharm], "base_link")

    def freeze_arm(self, whicharm):
        curr_angles = self.get_joint_angles()[whicharm]
        self.joint_movearm(whicharm, curr_angles)
    def cancel_goal(self,whicharm):
        self.control[whicharm].cancelGoal()
        self.control[whicharm].resetGoal()
     
    def cart_movearm(self,whicharm, pose, frame_id, \
            move_duration=5.0, ik_timeout=5.0, blocking=False):
        self.control[whicharm].cancelGoal()
        self.control[whicharm].resetGoal()
        if self.using_joint_controller[whicharm]:
            self.pr2_controller_manager.start_controller(whicharm, "cartesian")
            self.using_joint_controller[whicharm]= False
        pose = list(pose) + [500]*3 + [30]*3 + [False]*6 + [move_duration, frame_id]

        self.control[whicharm].addTrajectoryPoint(*pose)
        self.control[whicharm].sendGoal(wait=blocking)
        if blocking:
            self.control[whicharm].cancelGoal()
            self.control[whicharm].resetGoal()
        current_pose =  self.get_cartesian_pose()[whicharm]
        return check_cartesian_near_pose(current_pose, pose, 0.15, 0.1, self.tf_listener)
    ## Cartesian Move arm commands
    # pose is a 7 element list
    def cart_movearms(self, poses, frame_id, \
            move_duration=5.0, ik_timeout=5.0,blocking=True):
        for arm in self.arms:
            self.cart_movearm(arm, poses[arm], frame_id, move_duration,\
                    ik_timeout, False)
        if blocking:
            result = True
            for arm in self.arms:
                self.cart_client[arm].wait_for_result()
                #result  &= self.cart_client[arm].get_state()==3
            return result

if __name__=="__main__":
    rospy.init_node("simpletest")
    ctrl = ArmController()

    ctrl.joint_movearms({'r':[.1]*7, 'l':[.1]*7})
    curr = ctrl.get_cartesian_pose()
    ctrl.cart_movearms(curr, "base_link")
    raw_input("tested")

    print "cartesian pose is: %s" %  ctrl.get_cartesian_pose()
    print "joint angles are: %s" %  ctrl.get_joint_angles()
    print ctrl.joint_movearm("r", [0]*7)
    print ctrl.joint_movearm("l", [0]*7)
    print ctrl.cart_movearm('r',[.5,-.1,.8,0,0,0,1], "base_link")
    print ctrl.cart_movearm('r',[.5,-.0,.5,0,0,0,1], "base_link")
    print ctrl.cart_movearms(\
            {'r':[.5,-.5,.5,0,0,0,1], 'l':[.5, .5, .5, 0,0,0,1]}, "base_link")
    print ctrl.cart_movearms(\
            {'r':[.5,0,.5,0,0,0,1], 'l':[.5, -10, .5, 0,0,0,1]}, "base_link")
    print ctrl.joint_movearms({'r':[1]*7, 'l':[1]*7})