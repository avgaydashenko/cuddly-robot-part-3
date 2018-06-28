#!/usr/bin/env python

import rospy
import sys
from std_msgs.msg import Float32, ColorRGBA, Int32
from geometry_msgs.msg import PoseStamped, Twist, Vector3, Point
from ford_msgs.msg import PedTrajVec, NNActions, PlannerMode, Clusters
from visualization_msgs.msg import Marker, MarkerArray

import numpy as np
import numpy.matlib
import pickle
from matplotlib import cm
import matplotlib.pyplot as plt
import copy
import os
import time
import random
import math

import rospkg

import network
import agent
import util

PED_RADIUS = 0.3
# angle_1 - angle_2
# contains direction in range [-3.14, 3.14]
def find_angle_diff(angle_1, angle_2):
    angle_diff_raw = angle_1 - angle_2
    angle_diff = (angle_diff_raw + np.pi) % (2 * np.pi) - np.pi
    return angle_diff

class NN_jackal():
    def __init__(self, veh_name, veh_data, nn, actions):
        self.node_name = rospy.get_name()
        self.prev_other_agents_state = []

        # vehicle info
        self.veh_name = veh_name
        self.veh_data = veh_data

        # self.agent = agent.Agent(0.0, 0.0, 100.0, 100.0, radius, pref_speed, initial_heading, id)
        
        # neural network
        self.nn = nn
        self.actions = actions
        # self.value_net = value_net
        self.operation_mode = PlannerMode()
        self.operation_mode.mode = self.operation_mode.NN
        
        # for subscribers
        self.pose = PoseStamped()
        self.vel = Vector3()
        self.psi = 0.0
        self.ped_traj_vec = []
        self.other_agents_state = []
        self.feasible_actions = NNActions()

        # for publishers
        self.global_goal = PoseStamped()
        self.goal = PoseStamped()
        self.goal.pose.position.x = veh_data['goal'][0]
        self.goal.pose.position.y = veh_data['goal'][1]
        self.desired_position = PoseStamped()
        self.desired_action = np.zeros((2,))

        # handle obstacles close to vehicle's front
        self.stop_moving_flag = False
        self.d_min = 0.0
        self.new_subgoal_received = False
        self.new_global_goal_received = False

        # visualization
        self.path_marker = Marker()

        # Clusters
        self.prev_clusters = Clusters()
        self.current_clusters = Clusters()

        # subscribers and publishers
        self.num_poses = 0
        self.num_actions_computed = 0.0
        self.pub_others = rospy.Publisher('~other_vels',Vector3,queue_size=1)
        self.pub_twist = rospy.Publisher('~nn_cmd_vel',Twist,queue_size=1)
        self.pub_pose_marker = rospy.Publisher('~pose_marker',Marker,queue_size=1)
        self.pub_agent_marker = rospy.Publisher('~agent_marker',Marker,queue_size=1)
        self.pub_agent_markers = rospy.Publisher('~agent_markers',MarkerArray,queue_size=1)
        self.pub_path_marker = rospy.Publisher('~path_marker',Marker,queue_size=1)
        self.pub_goal_path_marker = rospy.Publisher('~goal_path_marker',Marker,queue_size=1)
        self.sub_pose = rospy.Subscriber('~pose',PoseStamped,self.cbPose)
        self.sub_vel = rospy.Subscriber('~velocity',Vector3,self.cbVel)
        self.sub_nn_actions = rospy.Subscriber('~safe_actions',NNActions,self.cbNNActions)
        self.sub_mode = rospy.Subscriber('~mode',PlannerMode, self.cbPlannerMode)
        self.sub_global_goal = rospy.Subscriber('~goal',PoseStamped, self.cbGlobalGoal)
        
        self.use_clusters = True
        # self.use_clusters = False
        if self.use_clusters:
            self.sub_clusters = rospy.Subscriber('~clusters',Clusters, self.cbClusters)
        else:
            self.sub_peds = rospy.Subscriber('~peds',PedTrajVec, self.cbPeds)

        # control timer
        self.control_timer = rospy.Timer(rospy.Duration(0.01),self.cbControl)
        self.nn_timer = rospy.Timer(rospy.Duration(0.1),self.cbComputeActionGA3C)

    def cbGlobalGoal(self,msg):
        self.new_global_goal_received = True
        self.global_goal = msg
        self.operation_mode.mode = self.operation_mode.SPIN_IN_PLACE

        self.goal.pose.position.x = msg.pose.position.x
        self.goal.pose.position.y = msg.pose.position.y
        self.goal.header = msg.header
        self.new_subgoal_received = True

    def cbNNActions(self,msg):
        # if msg.header.seq % 20 == 0:
        #     self.goal.pose.position.x = msg.subgoal.x
        #     self.goal.pose.position.y = msg.subgoal.y
        #     self.goal.header = msg.header
        #     self.new_subgoal_received = True
        self.feasible_actions = msg

    def cbPlannerMode(self, msg):
        self.operation_mode = msg
        self.operation_mode.mode = self.operation_mode.NN

    def cbPose(self, msg):
        self.num_poses += 1
        q = msg.pose.orientation
        self.psi = np.arctan2(2.0*(q.w*q.z + q.x*q.y), 1-2*(q.y*q.y+q.z*q.z)) # bounded by [-pi, pi]
        self.pose = msg
        self.visualize_pose(msg.pose.position,msg.pose.orientation)

    def cbVel(self, msg):
        self.vel = msg

    def cbPeds(self, msg):
        t_start = rospy.Time.now()
        self.ped_traj_vec = [ped_traj for ped_traj in msg.ped_traj_vec if len(ped_traj.traj) > 0]
        num_peds = len(self.ped_traj_vec)

        # compute relative position with respect to the Jackal
        rel_dist = np.zeros((num_peds, )) 
        rel_angle = np.zeros((num_peds, )) 
        # (rel_dist, angle)
        for i, ped_traj in enumerate(self.ped_traj_vec):
            rel_x = ped_traj.traj[-1].pose.x - self.pose.pose.position.x
            rel_y = ped_traj.traj[-1].pose.y - self.pose.pose.position.y
            rel_dist[i] = np.linalg.norm(np.array([rel_x, rel_y])) 
            rel_angle[i] = find_angle_diff(np.arctan2(rel_y, rel_x), self.psi)

        # ignore people in the back of Jackal (60 deg cone)
        valid_inds = np.where(abs(rel_angle)< 5.0 / 6.0 * np.pi)[0]

        # get the n closest agents
        self.other_agents_state = []
        if len(valid_inds) == 0:
            return
        else:
            if len(valid_inds) == 1:
                # print "valid_inds:", valid_inds
                # print "self.ped_traj_vec:", self.ped_traj_vec
                valid_inds = valid_inds[0]
                ped_traj_vec = [self.ped_traj_vec[valid_inds]]
                rel_dist = np.array([rel_dist[valid_inds]])
            elif len(valid_inds) > 1:
                # print 'before', len(self.ped_traj_vec)
                # print 'valid_inds', valid_inds
                ped_traj_vec = [self.ped_traj_vec[tt] for tt in valid_inds]
                # print 'after', len(self.ped_traj_vec)
                rel_dist = rel_dist[valid_inds]

            # sort other agents by rel_dist
            # num_neighbors = min(len(rel_dist), self.value_net.num_agents)
            # print 'num_neighbors', num_neighbors
            # print 'rel_dist', rel_dist
            # neighbor_inds = np.argpartition(rel_dist, num_neighbors)[:num_neighbors]
            if len(rel_dist) > self.value_net.num_agents-1:
                num_neighbors = self.value_net.num_agents-1
                neighbor_inds = np.argpartition(rel_dist, num_neighbors)[:num_neighbors]
            else:
                neighbor_inds = np.arange(len(rel_dist))
            # agent state: [pos.x, pos.y, vel.x, vel.y, heading_angle, pref_speed, \
            #            goals[0].x, goals[0].y, radius, turning_dir]
            for tt in neighbor_inds:
                ped_traj = ped_traj_vec[tt]
                # rel pos, rel vel, size
                x = ped_traj.traj[-1].pose.x; y = ped_traj.traj[-1].pose.y
                v_x = ped_traj.traj[-1].velocity.x; v_y = ped_traj.traj[-1].velocity.y
                radius = PED_RADIUS;turning_dir = 0.0
                # helper fields
                heading_angle = np.arctan2(v_y, v_x)
                pref_speed = np.linalg.norm(np.array([v_x, v_y]))
                goal_x = x + 5.0; goal_y = y + 5.0
                
                # filter speed
                alpha = 0.2
                for prev_other_agent_state in self.prev_other_agents_state:
                    pos_diff = np.linalg.norm(prev_other_agent_state[0:2] - np.array([x,y]))
                    heading_diff_abs = abs(find_angle_diff(prev_other_agent_state[4], heading_angle))
                    if pos_diff < 0.5 and heading_diff_abs < np.pi / 4.0:
                        v_x = alpha * v_x + (1-alpha) * prev_other_agent_state[2]
                        v_y = alpha * v_y + (1-alpha) * prev_other_agent_state[3]

                        # TODO: find the best match rather than the first match
                        break

                if pref_speed < 0.2:
                    pref_speed = 0; v_x = 0; v_y = 0
                other_agent_state = np.array([x, y, v_x, v_y, heading_angle, pref_speed, \
                    goal_x, goal_y, radius, turning_dir])
                self.other_agents_state.append(other_agent_state)

            self.prev_other_agents_state = copy.deepcopy(self.other_agents_state)
        t_end = rospy.Time.now()
        # print "cbPeds took:", (t_end - t_start).to_sec(), "sec"

    def cbClusters(self, msg):
        other_agents = []


        xs = []; ys = []; radii = []; labels = []
        num_clusters = len(msg.labels)
        for i in range(num_clusters):
            index = msg.labels[i]
            x = msg.mean_points[i].x; y = msg.mean_points[i].y
            v_x = msg.velocities[i].x; v_y = msg.velocities[i].y
            # radius = PED_RADIUS
            lower_r = np.linalg.norm(np.array([msg.mean_points[i].x-msg.min_points[i].x, msg.mean_points[i].y-msg.min_points[i].y]))
            upper_r = np.linalg.norm(np.array([msg.mean_points[i].x-msg.max_points[i].x, msg.mean_points[i].y-msg.max_points[i].y]))
            inflation_factor = 1.5
            radius = max(PED_RADIUS, inflation_factor * max(upper_r, lower_r))


            xs.append(x); ys.append(y); radii.append(radius); labels.append(index)
            # self.visualize_other_agent(x,y,radius,msg.labels[i])
            # helper fields
            heading_angle = np.arctan2(v_y, v_x)
            pref_speed = np.linalg.norm(np.array([v_x, v_y]))
            goal_x = x + 5.0; goal_y = y + 5.0
            
            # # filter speed
            # alpha = 0.2
            # for prev_other_agent_state in self.prev_other_agents_state:
            #     pos_diff = np.linalg.norm(prev_other_agent_state[0:2] - np.array([x,y]))
            #     heading_diff_abs = abs(find_angle_diff(prev_other_agent_state[4], heading_angle))
            #     if pos_diff < 0.5 and heading_diff_abs < np.pi / 4.0:
            #         v_x = alpha * v_x + (1-alpha) * prev_other_agent_state[2]
            #         v_y = alpha * v_y + (1-alpha) * prev_other_agent_state[3]

            #         # TODO: find the best match rather than the first match
            #         break
            if pref_speed < 0.2:
                pref_speed = 0; v_x = 0; v_y = 0
            other_agents.append(agent.Agent(x, y, goal_x, goal_y, radius, pref_speed, heading_angle, index))
        self.visualize_other_agents(xs, ys, radii, labels)
        self.other_agents_state = other_agents

    # def cbClustersOld(self, msg):
    #     other_agents = []


    #     self.prev_clusters = copy.deepcopy(self.current_clusters)
    #     self.current_clusters = msg
    #     self.clusters = []
    #     for i in range(len(self.current_clusters.labels)):
    #         moving = True
    #         # moving = np.linalg.norm([msg.velocities[i].x, \
    #             # msg.velocities[i].y]) > 0.3
    #         existing = True
    #         # if msg.labels[i] in self.prev_clusters.labels:
    #         #     prev_index = self.prev_clusters.labels.index(msg.labels[i])
    #         #     prev_vel = self.prev_clusters.velocities[prev_index]
    #         #     existing = np.linalg.norm([prev_vel.x, \
    #         #         prev_vel.y]) > 0.6
    #         # else:
    #         #     existing = False
    #         if moving and existing:
    #             self.clusters.append(msg.labels[i]) 
    #     # self.clusters = [msg.labels[i] \
    #     #     for i in range(len(msg.labels)) \
    #     #     if np.linalg.norm([msg.velocities[i].x, \
    #     #         msg.velocities[i].y]) > 0.6]
    #     num_peds = len(self.clusters)

    #     # compute relative position with respect to the Jackal
    #     rel_dist = np.zeros((num_peds, )) 
    #     rel_angle = np.zeros((num_peds, )) 
    #     # (rel_dist, angle)
    #     for i in range(num_peds):
    #         rel_x = msg.mean_points[i].x - self.pose.pose.position.x
    #         rel_y = msg.mean_points[i].y - self.pose.pose.position.y
    #         rel_dist[i] = np.linalg.norm(np.array([rel_x, rel_y])) 
    #         rel_angle[i] = find_angle_diff(np.arctan2(rel_y, rel_x), self.psi)

    #     # ignore people in the back of Jackal (60 deg cone)
    #     valid_inds = np.where(abs(rel_angle)< 4.0 / 6.0 * np.pi)[0]
    #     # valid_inds = np.where(abs(rel_angle)< 5.0 / 6.0 * np.pi)[0]
    #     # valid_inds = np.where((abs(rel_angle)< 5.0 / 6.0 * np.pi) & \
    #     #     (rel_dist < 3.0))[0]
    #     # get the n closest agents
    #     self.other_agents_state = []
    #     if len(valid_inds) == 0:
    #         return
    #     else:
    #         if len(valid_inds) == 1:
    #             valid_inds = valid_inds[0]
    #             ped_traj_vec = [self.clusters[valid_inds]]
    #             rel_dist = np.array([rel_dist[valid_inds]])
    #         elif len(valid_inds) > 1:
    #             # print 'before', len(self.ped_traj_vec)
    #             # print 'valid_inds', valid_inds
    #             ped_traj_vec = [self.clusters[tt] for tt in valid_inds]
    #             # print 'after', len(self.ped_traj_vec)
    #             rel_dist = rel_dist[valid_inds]

    #         # sort other agents by rel_dist
    #         # num_neighbors = min(len(rel_dist), self.value_net.num_agents)
    #         # print 'num_neighbors', num_neighbors
    #         # print 'rel_dist', rel_dist
    #         # neighbor_inds = np.argpartition(rel_dist, num_neighbors)[:num_neighbors]
    #         if len(rel_dist) > self.value_net.num_agents-1:
    #             num_neighbors = self.value_net.num_agents-1
    #             neighbor_inds = np.argpartition(rel_dist, num_neighbors)[:num_neighbors]
    #         else:
    #             neighbor_inds = np.arange(len(rel_dist))
    #         # agent state: [pos.x, pos.y, vel.x, vel.y, heading_angle, pref_speed, \
    #         #            goals[0].x, goals[0].y, radius, turning_dir]
    #         for tt in neighbor_inds:
    #             cluster_index = msg.labels.index(ped_traj_vec[tt])
    #             ped_traj = ped_traj_vec[tt]
    #             # rel pos, rel vel, size
    #             x = msg.mean_points[cluster_index].x; y = msg.mean_points[cluster_index].y
    #             v_x = msg.velocities[cluster_index].x; v_y = msg.velocities[cluster_index].y
    #             radius = PED_RADIUS;turning_dir = 0.0
    #             self.visualize_other_agent(x,y,msg.labels[cluster_index])
    #             # helper fields
    #             heading_angle = np.arctan2(v_y, v_x)
    #             pref_speed = np.linalg.norm(np.array([v_x, v_y]))
    #             goal_x = x + 5.0; goal_y = y + 5.0
                
    #             # filter speed
    #             alpha = 0.2
    #             for prev_other_agent_state in self.prev_other_agents_state:
    #                 pos_diff = np.linalg.norm(prev_other_agent_state[0:2] - np.array([x,y]))
    #                 heading_diff_abs = abs(find_angle_diff(prev_other_agent_state[4], heading_angle))
    #                 if pos_diff < 0.5 and heading_diff_abs < np.pi / 4.0:
    #                     v_x = alpha * v_x + (1-alpha) * prev_other_agent_state[2]
    #                     v_y = alpha * v_y + (1-alpha) * prev_other_agent_state[3]

    #                     # TODO: find the best match rather than the first match
    #                     break
    #             if pref_speed < 0.2:
    #                 pref_speed = 0; v_x = 0; v_y = 0
    #             # other_agent_state = np.array([x, y, v_x, v_y, heading_angle, pref_speed, \
    #                 # goal_x, goal_y, radius, turning_dir])
    #             # self.other_agents_state.append(other_agent_state)
    #             other_agents.append(agent.agent(x, y, goal_x, goal_y, pref_speed, radius, heading_angle, tt))
                
    #         self.prev_other_agents_state = copy.deepcopy(self.other_agents_state)
    #     return


    def stop_moving(self):
        twist = Twist()
        self.pub_twist.publish(twist)
        # print 'Stop Moving.'

    def update_action(self, action):
        # print 'update action'
        self.desired_action = action
        self.desired_position.pose.position.x = self.pose.pose.position.x + 1*action[0]*np.cos(action[1])
        self.desired_position.pose.position.y = self.pose.pose.position.y + 1*action[0]*np.sin(action[1])

        twist = Twist()
        twist.linear.x = action[0]
        yaw_error = action[1] - self.psi
        if yaw_error > np.pi:
            yaw_error -= 2*np.pi
        if yaw_error < -np.pi:
            yaw_error += 2*np.pi
        twist.angular.z = 2*yaw_error

    def find_vmax(self, d_min, heading_diff):
        # Calculate maximum linear velocity, as a function of error in
        # heading and clear space in front of the vehicle
        # (With nothing in front of vehicle, it's not important to
        # track MPs perfectly; with an obstacle right in front, the
        # vehicle must turn in place, then drive forward.)
        d_min = max(0.0,d_min)
        x = 0.3
        margin = 0.3
        # y = max(d_min - 0.3, 0.0)
        y = max(d_min, 0.0)
        # making sure x < y 
        if x > y:
            x = 0
        w_max = 1
        # x^2 + y^2 = (v_max/w_max)^2
        v_max = w_max * np.sqrt(x**2 + y**2)
        v_max = np.clip(v_max,0.0,self.veh_data['pref_speed'])
        # print 'V_max, x, y, d_min', v_max, x, y, d_min
        if abs(heading_diff) < np.pi / 18:
            return self.veh_data['pref_speed']
        return v_max

    def cbControl(self, event):
        if self.goal.header.stamp == rospy.Time(0) or self.stop_moving_flag \
            and not self.new_global_goal_received:
            self.stop_moving()
            return
        elif self.operation_mode.mode==self.operation_mode.NN:
            desired_yaw = self.desired_action[1]
            yaw_error = desired_yaw - self.psi
            if abs(yaw_error) > np.pi:
                yaw_error -= np.sign(yaw_error)*2*np.pi
            # print 'yaw_error:',yaw_error
            # max_yaw_error = 0.8
            # yaw_error = self.desired_action[1]
            gain = 2
            vw = gain*yaw_error

            use_d_min = False
            if True: 
                use_d_min = True
                # print "vmax:", self.find_vmax(self.d_min,yaw_error)
                vx = min(self.desired_action[0], self.find_vmax(self.d_min,yaw_error))
            else:
                vx = self.desired_action[0]
            # print "vx:", vx
            # elif abs(yaw_error) < max_yaw_error:
            #     vw = gain*yaw_error
            # else:
            #     vw = gain*max_yaw_error*np.sign(yaw_error)

            twist = Twist()
            twist.angular.z = vw
            twist.linear.x = vx
            self.pub_twist.publish(twist)
            self.visualize_action(use_d_min)
            return
        elif self.operation_mode.mode == self.operation_mode.SPIN_IN_PLACE:
            print 'Spinning in place.'
            self.stop_moving_flag = False
            angle_to_goal = np.arctan2(self.global_goal.pose.position.y - self.pose.pose.position.y, \
                self.global_goal.pose.position.x - self.pose.pose.position.x) 
            global_yaw_error = self.psi - angle_to_goal
            if abs(global_yaw_error) > 0.5:
                vx = 0.0
                vw = 1.0
                twist = Twist()
                twist.angular.z = vw
                twist.linear.x = vx
                self.pub_twist.publish(twist)
            else:
                print 'Done spinning in place'
                self.operation_mode.mode = self.operation_mode.NN
                self.new_global_goal_received = False
            return
        else:
            self.stop_moving()
            return

    def cbComputeActionGA3C(self, event):
        feasible_actions = copy.deepcopy(self.feasible_actions)
        if self.operation_mode.mode!=self.operation_mode.NN:
            print 'Not in NN mode'
            print self.operation_mode.mode
            return
        if len(feasible_actions.angles) == 0 \
            or len(feasible_actions.path_lengths)==0:
            print 'Invalid Feasible Actions'
            # print feasible_actions
            return


        # construct agent_state
        x = self.pose.pose.position.x; y = self.pose.pose.position.y
        v_x = self.vel.x; v_y = self.vel.y
        radius = self.veh_data['radius']; turning_dir = 0.0
        heading_angle = self.psi
        pref_speed = self.veh_data['pref_speed']
        goal_x = self.goal.pose.position.x; goal_y = self.goal.pose.position.y
        # in case current speed is larger than desired speed
        v = np.linalg.norm(np.array([v_x, v_y]))
        if v > pref_speed:
            v_x = v_x * pref_speed / v
            v_y = v_y * pref_speed / v

        host_agent = agent.Agent(x, y, goal_x, goal_y, radius, pref_speed, heading_angle, 0)
        host_agent.vel_global_frame = np.array([v_x, v_y])
        # host_agent.print_agent_info()

        other_agents_state = copy.deepcopy(self.other_agents_state)
        obs = host_agent.observe(other_agents_state)[1:]
        obs = np.expand_dims(obs, axis=0)
        # print "obs:", obs
        predictions = self.nn.predict_p(obs, None)[0]
        # print "predictions:", predictions
        # print "best action index:", np.argmax(predictions)
        raw_action = copy.deepcopy(self.actions[np.argmax(predictions)])
        action = np.array([pref_speed*raw_action[0], util.wrap(raw_action[1] + self.psi)])
        # print "raw_action:", raw_action
        # print "action:", action



        # feasible_actions
        angles = (np.array(feasible_actions.angles) + np.pi) % (2 * np.pi) - np.pi
        max_ranges = np.array(feasible_actions.max_speeds) - 0.3
        path_lengths = np.array(feasible_actions.path_lengths)
        # Sort the feasible actions by increasing angle
        order_inds = np.argsort(angles)
        max_ranges = max_ranges[order_inds]
        angles = angles[order_inds]
        path_lengths = path_lengths[order_inds]

        # Find which index corresponds to straight in front, and 90 deg each side
        zero_ind = np.digitize([self.psi+0.01], angles) - 1
        self.d_min = max_ranges[zero_ind]
        # self.d_min = 100.0


        # if close to goal
        kp_v = 0.5
        kp_r = 1   

        if host_agent.dist_to_goal < 2.0: # and self.percentComplete>=0.9:
            # print "somewhat close to goal"
            pref_speed = max(min(kp_v * (host_agent.dist_to_goal-0.1), pref_speed), 0.0)
            action[0] = min(raw_action[0], pref_speed)
            turn_amount = max(min(kp_r * (host_agent.dist_to_goal-0.1), 1.0), 0.0) * raw_action[1]
            action[1] = util.wrap(turn_amount + self.psi)
        if host_agent.dist_to_goal < 0.3:
            self.stop_moving_flag = True
        else:
            self.stop_moving_flag = False

        # print 'chosen action (rel angle)', action[0], action[1]
        self.update_action(action)


    def cbComputeAction(self, event):
        t_start = rospy.Time.now()
        self.num_actions_computed += 1.0
        feasible_actions = copy.deepcopy(self.feasible_actions)
        if self.operation_mode.mode!=self.operation_mode.NN:
            print 'Not in NN mode'
            print self.operation_mode.mode
            return
        if len(feasible_actions.angles) == 0 \
            or len(feasible_actions.path_lengths)==0:
            print 'Invalid Feasible Actions'
            # print feasible_actions
            return


        # copy other_agents_state
        other_agents_state = copy.deepcopy(self.other_agents_state)
        
        # construct agent_state
        agent_state = np.zeros((10,))
        # rel pos, rel vel, size
        x = self.pose.pose.position.x; y = self.pose.pose.position.y
        v_x = self.vel.x; v_y = self.vel.y
        radius = self.veh_data['radius']; turning_dir = 0.0
        heading_angle = self.psi
        pref_speed = self.veh_data['pref_speed']
        goal_x = self.goal.pose.position.x; goal_y = self.goal.pose.position.y
        # in case current speed is larger than desired speed
        v = np.linalg.norm(np.array([v_x, v_y]))
        if v > pref_speed:
            v_x = v_x * pref_speed / v
            v_y = v_y * pref_speed / v

        agent_state = np.array([x, y, v_x, v_y, heading_angle, pref_speed, \
            goal_x, goal_y, radius, turning_dir])

        # feasible_actions
        angles = (np.array(feasible_actions.angles) + np.pi) % (2 * np.pi) - np.pi
        max_ranges = np.array(feasible_actions.max_speeds) - 0.3
        print 'len(feasible_actions.path_lengths)', len(feasible_actions.path_lengths)
        path_lengths = np.array(feasible_actions.path_lengths)
        # Sort the feasible actions by increasing angle
        order_inds = np.argsort(angles)
        max_ranges = max_ranges[order_inds]
        angles = angles[order_inds]
        path_lengths = path_lengths[order_inds]

        # Find which index corresponds to straight in front, and 90 deg each side
        zero_ind = np.digitize([self.psi+0.01], angles) - 1
        # psi_plus_ninety = self.psi+np.pi/4
        # if (psi_plus_ninety > np.pi or psi_plus_ninety < -np.pi):
        #     psi_plus_ninety -= np.sign(psi_plus_ninety)*2*np.pi
        # psi_minus_ninety = self.psi-np.pi/4
        # if (psi_minus_ninety > np.pi or psi_minus_ninety < -np.pi):
        #     psi_minus_ninety -= np.sign(psi_minus_ninety)*2*np.pi
        # plus_ninety_ind = np.digitize([psi_plus_ninety+0.01], angles) - 1
        # minus_ninety_ind = np.digitize([psi_minus_ninety+0.01], angles) - 1

        # # print 'ranges, angles:',np.vstack([max_ranges,angles]).transpose()
        # # print 'zero_ind:',zero_ind
        # # print 'angle: (psi,%s),(psi+90,%s),(psi-90,%s)'%(self.psi,psi_plus_ninety,psi_minus_ninety)
        # # print 'ind: (0,%s),(90,%s),(-90,%s)'%(zero_ind,plus_ninety_ind,minus_ninety_ind)
        
        # # Find min clear dist in front of vehicle

        # if plus_ninety_ind < minus_ninety_ind:
        #     # e.g. 9 and 27, want ind 27-9, aka [-2,-2,...,9]
        #     front_inds = range(minus_ninety_ind-len(angles),plus_ninety_ind+1)
        # else:
        #     # e.g. 27 and 9, want ind 27-9, aka []
        #     front_inds = range(minus_ninety_ind,plus_ninety_ind+1)
        # # print 'front inds:', front_inds
        # front_ranges = max_ranges.take(front_inds,mode='wrap') + 0.3
        # front_angles = angles.take(front_inds,mode='wrap') - self.psi
        # front_angles[front_angles<-np.pi] += 2*np.pi
        # front_angles[front_angles>np.pi] -= 2*np.pi
        # # print 'front:',np.vstack([front_ranges,front_angles]).transpose()
        # front_dist = front_ranges * np.cos(front_angles)
        # self.d_min = np.min(front_dist)
        self.d_min = max_ranges[zero_ind]
        # print 'self.d_min: ', self.d_min


        # assume evenly spaced increments and covers approx -pi to pi
        num_angles = len(angles)
        small_inds = np.where(max_ranges < 0.5)[0]
        max_ranges[small_inds] = 0.0
        if num_angles < 2:
            static_constraints = None
        else:
            static_constraints = np.zeros((num_angles+2,3))
            static_constraints[1:-1,0] = np.clip(max_ranges, 0, self.veh_data['pref_speed'])
            static_constraints[1:-1,1] = angles
            static_constraints[1:-1,2] = path_lengths
            # wrap around
            static_constraints[0,0] = static_constraints[-2,0] 
            static_constraints[-1,0] = static_constraints[1,0]

            angle_incr = static_constraints[2,1] - static_constraints[1,1]
            static_constraints[0,1] = static_constraints[1,1] - angle_incr
            static_constraints[-1,1] = static_constraints[-2,1] + angle_incr

            static_constraints[0,2] = static_constraints[1,2]
            static_constraints[-1,2] = static_constraints[-2,2]

        # if no other agent exists, pick action that 
        t_mid = rospy.Time.now()
        if len(other_agents_state) == 0:
            print 'in ros_nn, but not using nn'
            dt_forward = 1.0 #0.1
            EPS = 1e-5
            # assert(np.max(static_constraints[:,0])<self.veh_data['pref_speed']+EPS)
            # assert(np.min(static_constraints[:,0]) > -EPS)
            actions = self.value_net.find_feasible_actions(agent_state, static_constraints=static_constraints)
            # assert(np.max(actions[:,0])<self.veh_data['pref_speed']+EPS)
            # print 'feasible actions', actions
            actions_xy = actions.copy()
            actions_xy[:,0] = actions[:,0] * np.cos(actions[:,1])
            actions_xy[:,1] = actions[:,0] * np.sin(actions[:,1])
            # print 'actions_xy', actions_xy
            # print 'cur_pos', agent_state[0:2]
            # print 'goal', agent_state[6:8]
            dist_2_goal = np.linalg.norm(agent_state[0:2] + \
                actions_xy * dt_forward - agent_state[6:8], axis=1)
            best_ind = np.argmin(dist_2_goal)
            # print 'dist_2_goal', dist_2_goal
            # print 'best_ind', best_ind
            # print 'min_dist', dist_2_goal[best_ind]
            action = actions[best_ind,:]
            if action[0] < EPS:
                angle_error = np.arctan2(agent_state[7] - agent_state[1], \
                                        agent_state[6] - agent_state[0])
                action[1] = np.clip(angle_error, -1.0, 1.0)

            subgoal = np.array([self.goal.pose.position.x,self.goal.pose.position.y])
            self.visualize_subgoal(subgoal)
            # print 'best_action', action
        else:
            # print 'in ros_nn, and using nn'
            # scale veh's radius
            min_dists = [np.linalg.norm(agent_state[0:2]-other_agent_state[0:2]) \
                for other_agent_state in other_agents_state]
            min_dist = min(min_dists)
            if min_dist < 1.5:
                agent_state[8] = self.veh_data['radius'] * 0.6
            elif min_dist < 2.0:
                agent_state[8] = self.veh_data['radius'] * 0.8
            # print 'static_constr:', static_constraints
            # action = self.value_net.find_next_action(agent_state, \
            #     other_agents_state, static_constraints=static_constraints)

            ### Select Subgoal based on NN
            if self.new_subgoal_received:
                print 'Looking for Subgoal...'
                self.new_subgoal_received = False
                subgoal, subgoal_options = self.value_net.find_subgoal(agent_state, \
                    other_agents_state, min_dist, static_constraints=static_constraints)
                agent_state[6:8] = copy.deepcopy(subgoal)
                self.update_subgoal(subgoal)
                self.visualize_subgoal(subgoal, subgoal_options=subgoal_options)
                # print 'subgoal:', subgoal - agent_state[0:2]
            else:
                print 'Not looking for subgoal'
            # subgoal = np.array([self.goal.pose.position.x,self.goal.pose.position.y])
            # self.visualize_subgoal(subgoal)


            action = self.value_net.find_next_action(agent_state, \
                other_agents_state, static_constraints=static_constraints)
            t_mid = rospy.Time.now()
        # print 'cbComputeAction 3'

        # if close to goal
        dist_2_goal = np.linalg.norm(np.array([x-goal_x, y-goal_y]))
        kp_v = 0.5
        kp_r = 1   

        if dist_2_goal < 2.0: # and self.percentComplete>=0.9:
            pref_speed = max(min(kp_v * (dist_2_goal-0.1), pref_speed), 0.0)
            # action[1] = max(min(kp_r * (dist_2_goal-0.1), 1.0), 0.0) * action[1]
            heading_diff = find_angle_diff(action[1], self.psi)
            # print 'nn heading_diff', heading_diff
            action[1] = max(min(kp_r * (dist_2_goal-0.1), 1.0), 0.0) * heading_diff + \
                self.psi
            action[1] = (action[1] + np.pi) % ( 2 * np.pi) - np.pi
            action[0] = min(action[0], pref_speed)
        if dist_2_goal < 0.3:
            self.stop_moving_flag = True
        else:
            self.stop_moving_flag = False


        # np.set_printoptions(precision=3)
        np.set_printoptions(formatter={'float_kind':'{:2f}'.format})
        # print '---> agent_state'
        # print agent_state
        # print 'other_agents_state'
        # for other_agent_state in other_agents_state:
            # if np.linalg.norm(other_agent_state[2:4]) > 0.5:
                # print other_agent_state
                # self.pub_others.publish(Vector3(x=other_agent_state[2],y=other_agent_state[3]))

        print 'chosen action (rel angle)', action[0], find_angle_diff(action[1],self.psi)
        self.update_action(action)
        
        t_end = rospy.Time.now()
        # print "cbComputeAction took:", (t_end - t_start).to_sec(), "sec"
        # print "cbComputeAction begining took:", (t_mid - t_start)/(t_end - t_start), "pct"


    def update_subgoal(self,subgoal):
        self.goal.pose.position.x = subgoal[0]
        self.goal.pose.position.y = subgoal[1]

    def visualize_subgoal(self,subgoal, subgoal_options=None):
        markers = MarkerArray()

        # Display GREEN DOT at NN subgoal
        marker = Marker()
        marker.header.stamp = rospy.Time.now()
        marker.header.frame_id = 'map'
        marker.ns = 'subgoal'
        marker.id = 0
        marker.type = marker.CUBE
        marker.action = marker.ADD
        marker.pose.position.x = subgoal[0]
        marker.pose.position.y = subgoal[1]
        marker.scale = Vector3(x=0.4,y=0.4,z=0.2)
        marker.color = ColorRGBA(g=1.0,a=1.0)
        marker.lifetime = rospy.Duration(2.0)
        self.pub_goal_path_marker.publish(marker)

        if subgoal_options is not None:
            for i in xrange(len(subgoal_options)):
                marker = Marker()
                marker.header.stamp = rospy.Time.now()
                marker.header.frame_id = 'map'
                marker.ns = 'subgoal'
                marker.id = i+1
                marker.type = marker.CUBE
                marker.action = marker.ADD
                marker.pose.position.x = subgoal_options[i][0]
                marker.pose.position.y = subgoal_options[i][1]
                marker.scale = Vector3(x=0.2,y=0.2,z=0.2)
                marker.color = ColorRGBA(b=1.0,r=1.0,a=1.0)
                marker.lifetime = rospy.Duration(1.0)
                self.pub_goal_path_marker.publish(marker)




    def visualize_pose(self,pos,orientation):
        # Yellow Box for Vehicle
        marker = Marker()
        marker.header.stamp = rospy.Time.now()
        marker.header.frame_id = 'map'
        marker.ns = 'agent'
        marker.id = 0
        marker.type = marker.CUBE
        marker.action = marker.ADD
        marker.pose.position = pos
        marker.pose.orientation = orientation
        marker.scale = Vector3(x=0.7,y=0.42,z=1)
        marker.color = ColorRGBA(r=1.0,g=1.0,a=1.0)
        marker.lifetime = rospy.Duration(1.0)
        self.pub_pose_marker.publish(marker)

        # Red track for trajectory over time
        marker = Marker()
        marker.header.stamp = rospy.Time.now()
        marker.header.frame_id = 'map'
        marker.ns = 'agent'
        marker.id = self.num_poses
        marker.type = marker.CUBE
        marker.action = marker.ADD
        marker.pose.position = pos
        marker.pose.orientation = orientation
        marker.scale = Vector3(x=0.2,y=0.2,z=0.2)
        marker.color = ColorRGBA(r=1.0,a=1.0)
        marker.lifetime = rospy.Duration(10.0)
        self.pub_pose_marker.publish(marker)

    def visualize_other_agents(self,xs,ys,radii,labels):
        markers = MarkerArray()
        for i in range(len(xs)):
            # Orange box for other agent
            marker = Marker()
            marker.header.stamp = rospy.Time.now()
            marker.header.frame_id = 'map'
            marker.ns = 'other_agent'
            marker.id = labels[i]
            marker.type = marker.CYLINDER
            marker.action = marker.ADD
            marker.pose.position.x = xs[i]
            marker.pose.position.y = ys[i]
            # marker.pose.orientation = orientation
            marker.scale = Vector3(x=2*radii[i],y=2*radii[i],z=1)
            marker.color = ColorRGBA(r=1.0,g=0.4,a=1.0)
            marker.lifetime = rospy.Duration(0.1)
            markers.markers.append(marker)

        self.pub_agent_markers.publish(markers)

    def visualize_action(self, use_d_min):
        # Display BLUE ARROW from current position to NN desired position
        marker = Marker()
        marker.header.stamp = rospy.Time.now()
        marker.header.frame_id = 'map'
        marker.ns = 'path_arrow'
        marker.id = 0
        marker.type = marker.ARROW
        marker.action = marker.ADD
        marker.points.append(self.pose.pose.position)
        marker.points.append(self.desired_position.pose.position)
        marker.scale = Vector3(x=0.1,y=0.2,z=0.2)
        marker.color = ColorRGBA(b=1.0,a=1.0)
        marker.lifetime = rospy.Duration(0.5)
        self.pub_goal_path_marker.publish(marker)

        # Display BLUE DOT at NN desired position
        marker = Marker()
        marker.header.stamp = rospy.Time.now()
        marker.header.frame_id = 'map'
        marker.ns = 'path_trail'
        marker.id = self.num_poses
        marker.type = marker.CUBE
        marker.action = marker.ADD
        marker.pose.position = copy.deepcopy(self.desired_position.pose.position)
        marker.scale = Vector3(x=0.2,y=0.2,z=0.2)
        marker.color = ColorRGBA(b=1.0,a=0.1)
        marker.lifetime = rospy.Duration(0.5)
        if self.desired_action[0] == 0.0:
            marker.pose.position.x += 2.0*np.cos(self.desired_action[1])
            marker.pose.position.y += 2.0*np.sin(self.desired_action[1])
        self.pub_goal_path_marker.publish(marker)

        # Display RED LINE from along minimum clear distance in front
        # marker = Marker()
        # marker.header.stamp = rospy.Time.now()
        # marker.header.frame_id = 'map'
        # marker.ns = 'clear_distance'
        # marker.id = 0
        # marker.type = marker.LINE_LIST
        # marker.lifetime = rospy.Duration(0.5)
        # marker.scale = Vector3(x=0.08,y=0.08,z=0.08)
        # if use_d_min:
        #     marker.color = ColorRGBA(r=1.0,a=1.0)
        # else:
        #     marker.color = ColorRGBA(r=1.0,g=1.0,a=1.0)
        # x_midpt = self.pose.pose.position.x + self.d_min*np.cos(self.psi)
        # y_midpt = self.pose.pose.position.y + self.d_min*np.sin(self.psi)
        # x_max = x_midpt - 1*np.sin(self.psi)
        # x_min = x_midpt + 1*np.sin(self.psi)
        # y_max = y_midpt + 1*np.cos(self.psi)
        # y_min = y_midpt - 1*np.cos(self.psi)
        # marker.points.append(Point(x=x_max,y=y_max))
        # marker.points.append(Point(x=x_min,y=y_min))
        # self.pub_goal_path_marker.publish(marker)


    def on_shutdown(self):
        rospy.loginfo("[%s] Shutting down." %(self.node_name))
        self.stop_moving()
        rospy.loginfo("Stopped %s's velocity." %(self.veh_name))


def run():
    print 'hello world from cadrl_node.py'
    file_dir = os.path.dirname(os.path.realpath(__file__))
    plt.rcParams.update({'font.size': 18})
    rospack = rospkg.RosPack()

    a = network.Actions()
    actions = a.actions
    num_actions = a.num_actions
    nn = network.NetworkVP_rnn(network.Config.DEVICE, 'network', num_actions)
    nn.simple_load(rospack.get_path('cadrl_ros')+'/checkpoints/network_01900000')

    rospy.init_node('nn_jackal',anonymous=False)
    veh_name = 'JA01'
    pref_speed = rospy.get_param("~jackal_speed")
    veh_data = {'goal':np.zeros((2,)),'radius':0.5,'pref_speed':pref_speed,'kw':10.0,'kp':1.0,'name':'JA01'}

    print "********\n*******\n*********\nJackal speed:", pref_speed, "\n**********\n******"

    nn_jackal = NN_jackal(veh_name, veh_data, nn, actions)
    rospy.on_shutdown(nn_jackal.on_shutdown)

    rospy.spin()

 


if __name__ == '__main__':
    run()
