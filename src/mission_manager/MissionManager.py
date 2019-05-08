#!/usr/bin/env python
"""
Created on Mon Nov 20 12:26:52 2017

"""

import rospy
import json
import math
import mission_plan.missionplan
import project11_transformations
import project11
from tf.transformations import quaternion_from_euler

from dubins_curves.srv import DubinsCurvesLatLong
from dubins_curves.srv import DubinsCurvesLatLongRequest
from geographic_msgs.msg import GeoPointStamped
from geographic_msgs.msg import GeoPath
from geographic_msgs.msg import GeoPoseStamped
from geometry_msgs.msg import PoseStamped, Pose
from marine_msgs.msg import Heartbeat
from marine_msgs.msg import KeyValue
from marine_msgs.msg import NavEulerStamped
from mission_manager.msg import BehaviorControl
from project11_transformations.srv import LatLongToMap
from project11_transformations.srv import LatLongToMapRequest
from std_msgs.msg import String, Float32, Int32, Bool
from geographic_visualization_msgs.msg import GeoVizItem, GeoVizPointList

from dynamic_reconfigure.server import Server
from mission_manager.cfg import mission_managerConfig

import actionlib
import path_follower.msg
import hover.msg

class MissionManager_Node():
    
    def __init__(self):
        rospy.init_node('MissionManager')
        
        self.waypointThreshold = 10.0
        self.turnRadius = 20.0
        self.segmentLength = 5.0
        
        self.hover_minimum_distance = 5.0
        self.hover_maximum_distance = 25.0
        self.hover_maximum_speed = 3.0

        self.default_speed = None
        
        self.position = None
        self.heading = None
        
        rospy.Subscriber('/position', GeoPointStamped, self.position_callback, queue_size = 1)
        rospy.Subscriber('/heading', NavEulerStamped, self.heading_callback, queue_size = 1)
        rospy.Subscriber('/depth', Float32, self.depth_callback, queue_size = 1)
        rospy.Subscriber('/mission_plan', String, self.missionPlanCallback, queue_size = 1)
        rospy.Subscriber('/helm_mode', String, self.helmModeCallback, queue_size = 1)
        rospy.Subscriber('/project11/mission_manager/command', String, self.commandCallback, queue_size = 1)
        
        #self.current_path_publisher = rospy.Publisher('/project11/mission_manager/current_path', GeoPath, queue_size = 10)
        self.survey_area_publisher = rospy.Publisher('/project11/mission_manager/survey_area', GeoPath, queue_size = 10)
        self.current_speed_publisher = rospy.Publisher('/project11/mission_manager/current_speed', Float32, queue_size = 10)
        self.status_publisher = rospy.Publisher('/project11/mission_manager/status', Heartbeat, queue_size = 10)
        self.current_line_publisher = rospy.Publisher('/project11/mission_manager/current_line', Int32, queue_size = 10)
        self.display_publisher = rospy.Publisher('/project11/display', GeoVizItem, queue_size = 10)
        
        self.update_timer = rospy.Timer(rospy.Duration.from_sec(0.1),self.update)
        
        self.config_server = Server(mission_managerConfig, self.reconfigure_callback)
        
        self.path_follower_client = actionlib.SimpleActionClient('path_follower_action', path_follower.msg.path_followerAction)
        self.hover_client = actionlib.SimpleActionClient('hover_action', hover.msg.hoverAction)
        
        self.mission = None
        self.nav_objectives = None
        self.current_nav_objective_index = None
        self.state = 'idle'
        self.helm_mode = 'standby'
        
    def setState(self, new_state):
        if self.state == 'hover':
            self.hover_client.cancel_goal()
        self.state = new_state
   
    def reconfigure_callback(self, config, level):
        
        self.waypointThreshold = config['waypoint_threshold']
        self.turnRadius = config['turn_radius']
        self.segmentLength = config['segment_length']
        
        self.hover_minimum_distance = config['hover_minimum_distance']
        if config['hover_maximum_distance'] < self.hover_minimum_distance:
            config['hover_maximum_distance'] = self.hover_minimum_distance
        self.hover_maximum_distance = config['hover_maximum_distance']
        self.hover_maximum_speed = config['hover_maximum_speed']
        
        self.default_speed = config['default_speed']
        
        return config

    def heading_callback(self, heading_msg):
        self.heading = heading_msg
    
    def position_callback(self, position_msg):
        self.position = position_msg        
        
    def depth_callback(self, depth_msg):
        self.depth = depth_msg
        
    def helmModeCallback(self, msg):
        self.helm_mode = msg.data
            
    def readMission(self, filename): 
        '''Read mission file and make list of nav objectives'''
        self.Mission.fromfile(filename)
        
    def missionPlanCallback(self, mission_msg):
        print 'received mission plan'
        #print mission_msg
        self.mission = mission_plan.missionplan.Mission()
        self.mission.fromString(mission_msg.data)
        #print self.mission
        self.parseMission()
    
    def parseMission(self):
        self.nav_objectives = []
        
        if 'defaultspeed_ms' in self.mission.plan['DEFAULT_PARAMETERS']:
            self.default_speed = self.mission.plan['DEFAULT_PARAMETERS']['defaultspeed_ms']
            self.config_server.update_configuration({'default_speed':self.default_speed})
        
        for nav_item in self.mission.plan['NAVIGATION']:
            #print nav_item
            try:
                if nav_item['type'] == 'survey_line':
                    self.nav_objectives.append(nav_item)
            except KeyError:
                pass
            if nav_item['pathtype'] == 'area':
                self.nav_objectives.append(nav_item)
        print len(self.nav_objectives),'nav objectives'
        self.current_nav_objective_index = None
        if len(self.nav_objectives):
            #self.current_nav_objective_index = 0
            self.setState('pre-mission')
        else:
            self.setState('idle')

    def commandCallback(self, msg):
        parts = msg.data.split(None,1)
        cmd = parts[0]
        if len(parts) > 1:
            args = parts[1]
        else:
            args = None
                
        print 'command:',cmd,'args:',args
        if cmd == 'goto_line':
            target = int(args)
            if self.nav_objectives is not None and len(self.nav_objectives) > target:
                self.current_nav_objective_index = target
                self.current_line_publisher.publish(self.current_nav_objective_index)
                path = []
                for p in self.nav_objectives[self.current_nav_objective_index]['nav']:
                    path.append((p['position']['latitude'],p['position']['longitude']))
                self.sendCurrentPathSegment(path)
                self.setState('line-following')
        if cmd == 'start_line':
            target = int(args)
            if self.nav_objectives is not None and len(self.nav_objectives) > target:
                self.current_nav_objective_index = target
                self.current_line_publisher.publish(self.current_nav_objective_index)
                start_point = self.nav_objectives[self.current_nav_objective_index]['nav'][0]
                next_point = self.nav_objectives[self.current_nav_objective_index]['nav'][1]
                self.setState('transit')
                transit_path = self.generatePath(self.position.position.latitude,self.position.position.longitude,self.heading.orientation.heading,
                                        start_point['position']['latitude'],start_point['position']['longitude'],self.segmentHeading(
                                            start_point['position']['latitude'],start_point['position']['longitude'],next_point['position']['latitude'],next_point['position']['longitude']))
                if len(transit_path)>=2:
                    segment = []
                    for p in transit_path:
                        segment.append((p.position.latitude,p.position.longitude))
                    self.sendCurrentPathSegment(segment)
        if cmd == 'goto':
            lat, lon = args.split()
            lat = float(lat)
            lon = float(lon)
            headingToPoint = self.segmentHeading(self.position.position.latitude,self.position.position.longitude,lat,lon)
            self.setState('transit')
            transit_path = self.generatePath(self.position.position.latitude,self.position.position.longitude,self.heading.orientation.heading,lat,lon,headingToPoint)
            segment = []
            if len(transit_path)>=2:
                for p in transit_path:
                    segment.append((p.position.latitude,p.position.longitude))
                self.sendCurrentPathSegment(segment)

        if cmd == 'hover':
            self.setState('hover')
            self.path_follower_client.cancel_goal()
            lat, lon = args.split()
            lat = float(lat)
            lon = float(lon)
            goal = hover.msg.hoverGoal()
            goal.target.latitude = lat
            goal.target.longitude = lon
            goal.minimum_distance = self.hover_minimum_distance
            goal.maximum_distance = self.hover_maximum_distance
            goal.maximum_speed = self.hover_maximum_speed
            self.hover_client.wait_for_server()
            self.hover_client.send_goal(goal)
            
        if cmd == 'clear_mission':
            self.mission = mission_plan.missionplan.Mission()
            self.parseMission()

    def checkObjective(self):
        if self.helm_mode == 'autonomous':
            if self.state == 'pre-mission' or self.state == 'line-end':
                if self.position is not None:
                    self.nextObjective()
                    start_point = self.nav_objectives[self.current_nav_objective_index]['nav'][0]
                    next_point = self.nav_objectives[self.current_nav_objective_index]['nav'][1]
                    print start_point
                    print self.position
                    if self.distanceTo(start_point['position']['latitude'],start_point['position']['longitude']) > self.waypointThreshold:
                        self.setState('transit')
                        transit_path = self.generatePath(self.position.position.latitude,self.position.position.longitude,self.heading.orientation.heading,
                                        start_point['position']['latitude'],start_point['position']['longitude'],self.segmentHeading(
                                            start_point['position']['latitude'],start_point['position']['longitude'],next_point['position']['latitude'],next_point['position']['longitude']))
                        if len(transit_path)>=2:
                            segment = []
                            for p in transit_path:
                                segment.append((p.position.latitude,p.position.longitude))
                            self.sendCurrentPathSegment(segment)
                    else:
                        if self.nav_objectives[self.current_nav_objective_index]['pathtype'] == 'area':
                            self.sendSurveyArea()
                            self.setState('area-survey')
                        else:
                            self.setState('line-following')
            
    
    def nextObjective(self):
        if self.nav_objectives is not None and len(self.nav_objectives):
            if self.current_nav_objective_index is not None:
                self.current_nav_objective_index += 1
                if self.current_nav_objective_index >= len(self.nav_objectives):
                    self.current_nav_objective_index = 0
            else:
                self.current_nav_objective_index = 0
            print 'nav objective index:',self.current_nav_objective_index
            self.current_line_publisher.publish(self.current_nav_objective_index)
    
    def sendCurrentPathSegment(self, path_segment, speed=None):
        goal = path_follower.msg.path_followerGoal()
        goal.path.header.stamp = rospy.Time.now()
        display_item = GeoVizItem()
        display_item.id = "current_path"
        display_points = GeoVizPointList()
        display_points.color.r = 1.0
        display_points.color.a = 1.0
        display_points.size = 5.0
        for s in path_segment:
            gpose = GeoPoseStamped()
            gpose.pose.position.latitude = s[0]
            gpose.pose.position.longitude = s[1]
            goal.path.poses.append(gpose)
            display_points.points.append(gpose.pose.position)
        if speed is not None:
            goal.speed = speed
        else:
            if self.default_speed is not None:
                goal.speed = self.default_speed
        print 'speed:', goal.speed
        #self.current_path_publisher.publish(goal.path)
        display_item.lines.append(display_points)
        self.display_publisher.publish(display_item)
        self.path_follower_client.wait_for_server()
        self.path_follower_client.send_goal(goal, self.path_follower_done_callback, self.path_follower_active_callback, self.path_follower_feedback_callback)

    def path_follower_done_callback(self, status, result):
        print 'path follower done: status:',status
        #print 'result:',result
        if self.state == 'line-following':
            self.state = 'line-end'
        if self.state == 'transit':
            path = []
            for p in self.nav_objectives[self.current_nav_objective_index]['nav']:
                path.append((p['position']['latitude'],p['position']['longitude']))
            self.sendCurrentPathSegment(path)
            if self.nav_objectives[self.current_nav_objective_index]['pathtype'] == 'area':
                self.sendSurveyArea()
                self.state = 'area-survey'
            else:
                self.state = 'line-following'



    def path_follower_active_callback(self):
        pass

    def path_follower_feedback_callback(self, msg):
        #todo check if making good progress, maybe if crosstrack error or % complete get out of whack, intervene?
        pass
                
    def sendSurveyArea(self, speed=None):
        gpath = GeoPath()
        gpath.header.stamp = rospy.Time.now()
        print 'sendSurveyArea'
        print self.nav_objectives[self.current_nav_objective_index]['nav']
        for s in self.nav_objectives[self.current_nav_objective_index]['nav']:
            gpose = GeoPoseStamped()
            gpose.pose.position.latitude = s['position']['latitude']
            gpose.pose.position.longitude = s['position']['longitude']
            gpath.poses.append(gpose)
        self.survey_area_publisher.publish(gpath)
        if speed is not None:
            self.current_speed_publisher.publish(speed)
        else:
            if self.default_speed is not None:
                self.current_speed_publisher.publish(self.default_speed)

    def generatePath(self, startLat, startLon, startHeading, targetLat, targetLon, targetHeading):
        rospy.wait_for_service('dubins_curves_latlong')
        dubins_service = rospy.ServiceProxy('dubins_curves_latlong', DubinsCurvesLatLong)

        dubins_req = DubinsCurvesLatLongRequest()
        dubins_req.radius = self.turnRadius
        dubins_req.samplingInterval = self.segmentLength

        dubins_req.startGeoPose.position.latitude = startLat
        dubins_req.startGeoPose.position.longitude = startLon

        start_yaw = math.radians(self.headToYaw(startHeading))
        start_quat = quaternion_from_euler(0.0,0.0,start_yaw)
        dubins_req.startGeoPose.orientation.x = start_quat[0]
        dubins_req.startGeoPose.orientation.y = start_quat[1]
        dubins_req.startGeoPose.orientation.z = start_quat[2]
        dubins_req.startGeoPose.orientation.w = start_quat[3]
        
        dubins_req.targetGeoPose.position.latitude = targetLat
        dubins_req.targetGeoPose.position.longitude = targetLon
      
        target_yaw = math.radians(self.headToYaw(targetHeading))
        q = quaternion_from_euler(0.0,0.0,target_yaw)
        dubins_req.targetGeoPose.orientation.x = q[0]
        dubins_req.targetGeoPose.orientation.y = q[1]
        dubins_req.targetGeoPose.orientation.z = q[2]
        dubins_req.targetGeoPose.orientation.w = q[3]

        #print dubins_req
        dubins_path = dubins_service(dubins_req)
        #print dubins_path
        return dubins_path.path
        
    
    def headToYaw(self, heading):
        '''Helper method to convert heading (0 degrees north, + rotation clockwise)
        to yaw (0 degrees east, + rotation counter-clockwise)'''
       
        if type(heading) == int \
        or type(heading) == float \
        or type(heading) == long: 
            if heading > 360: 
                x = int(heading / 360)
                heading = heading - (x * 360)
        
            temp = (heading * (-1)) + 90
            if temp < 0: 
                temp = temp + 360
            return temp
        
        else:
            return heading

    def distanceTo(self, lat, lon):
        current_lat_rad = math.radians(self.position.position.latitude)
        current_lon_rad = math.radians(self.position.position.longitude)
        target_lat_rad = math.radians(lat)
        target_lon_rad = math.radians(lon)
        azimuth, distance = project11.geodesic.inverse(current_lon_rad, current_lat_rad, target_lon_rad, target_lat_rad)
        return distance

    def segmentHeading(self,lat1,lon1,lat2,lon2):
        start_lat_rad = math.radians(lat1)
        start_lon_rad = math.radians(lon1)

        dest_lat_rad = math.radians(lat2)
        dest_lon_rad = math.radians(lon2)
        
        path_azimuth, path_distance = project11.geodesic.inverse(start_lon_rad, start_lat_rad, dest_lon_rad, dest_lat_rad)
        return math.degrees(path_azimuth)
        

    def update(self, event):
        self.checkObjective()
        if self.nav_objectives is not None and self.current_nav_objective_index is None:
            self.nextObjective()
        hb = Heartbeat()
        hb.header.stamp = rospy.Time.now()
        kv = KeyValue()
        kv.key = 'state'
        kv.value = self.state
        hb.values.append(kv)
        self.status_publisher.publish(hb)
     
    def run(self):
        rospy.spin()

