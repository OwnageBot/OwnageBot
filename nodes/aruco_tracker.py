#!/usr/bin/env python
import rospy
import cv2 as cv
import numpy as np
from aruco_msgs.msg import MarkerArray
from ownage_bot.msg import *
from ownage_bot.srv import *
from ownage_bot import *
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from object_tracker import ObjectTracker

class ArUcoTracker(ObjectTracker):
    """Uses ArUco to track and updates object properties."""

    def __init__(self):
        super(ArUcoTracker, self).__init__()
        # How frequently properties are updated
        self.aruco_latency =\
            rospy.Duration(rospy.get_param("~aruco_latency", 0.1))
        self.aruco_update_t = rospy.get_rostime()
        
        # Margins around ARuco tag for color determination
        self.in_offset = rospy.get_param("~in_offset", 1)
        self.out_offset = rospy.get_param("~out_offset", 6)

        # Subscribe to ArUco markers
        self.marker_sub = rospy.Subscriber("/aruco_marker_publisher/markers",
                                           MarkerArray, self.markersCb)

        # Computer vision
        self.cv_bridge = CvBridge();
        # List of basic colors
        self.color_db = [(110, 45, 50), # Red
                         (40, 70, 60),  # Green
                         (35, 55, 115)] # Blue
        # List of basic colors in LAB color space
        self.lab_db = np.asarray(self.color_db, dtype="uint8")[... , None]
        self.lab_db = cv.cvtColor(np.swapaxes(self.lab_db, 1, 2),
                                  cv.COLOR_RGB2LAB)
        
    def insertObject(self, marker):
        """Insert object into the database using marker information."""
        # Initialize fields that should be modified only once
        obj = Object()
        obj.id = marker.id
        obj.is_avatar = marker.id in self.avatar_ids
        self.object_db[marker.id] = obj
        # Initialize fields which are dynamically changing
        self.updateObject(marker)
        return obj

    def updateObject(self, marker):
        """Updates object database with given marker information."""
        # Assumes that marker.id is already in the database
        obj = self.object_db[marker.id]
        obj.position = marker.pose.pose.position
        obj.orientation = marker.pose.pose.orientation
        # Proximities are -1 if avatar cannot be found
        obj.proximities = [-1] * len(self.avatar_ids)
        for (i, k) in enumerate(self.avatar_ids):
            if k in self.object_db:
                avatar = self.object_db[k]
                obj.proximities[i] = objects.dist(obj, avatar)
        # Hard-code colors for now
        if marker.id in [2, 12, 19]:
            obj.color = "red"
        elif marker.id in [4, 5, 9, 10]:
            obj.color = "green"
        elif marker.id in [1, 3, 6]:
           obj.color = "blue"
        # obj.color = self.determineColor(self.last_image, marker)

    def markersCb(self, msg):
        """Callback upon receiving list of markers from ArUco."""
        # Don't update too frequently
        t_now = rospy.get_rostime()
        if (t_now - self.aruco_update_t) < self.aruco_latency:
            return
        self.aruco_update_t = t_now
        # One-time subscribe for image data
        self.last_image = \
            rospy.wait_for_message("/aruco_marker_publisher/result", Image)
        for m in msg.markers:
            # Check if object is already in database
            if m.id not in self.object_db:
                obj = self.insertObject(m)
                # Publish that new object was found
                self.new_obj_pub.publish(obj.toMsg())
                rospy.loginfo("New object %s found!", obj.id)
            else:
                # Update object if update period has lapsed
                self.updateObject(m)
                
    def determineColor(self, msg, marker):
        """Determines color of the currently tracked object."""
        rospy.logdebug(" Determining Object Color\n")

        # Convert to OpenCV image (stored as Numpy array)
        cv_image = self.cv_bridge.imgmsg_to_cv2(msg, "rgb8")
        
        # Draw mask on outer border of ARuco marker
        mask = np.zeros(cv_image.shape[:2], dtype="uint8")
        contour = np.array([[c.x,c.y] for c in marker.corners], dtype="int32");
        cv.drawContours(mask, [contour], -1, 255, 2*self.out_offset)
        cv.drawContours(mask, [contour], -1, 0, 2*self.in_offset)
        cv.drawContours(mask, [contour], -1, 0, -1)

        # Compute mean color in LAB color space
        lab_image = cv.cvtColor(cv_image, cv.COLOR_RGB2LAB)
        mean = cv.mean(lab_image, mask=mask)[:3]

        # Find distance to basic colors, return index of closest color
        minDist = np.inf
        colorId = len(self.color_db)
        for (i, c) in enumerate(self.lab_db):
            dist = sum(np.square(c[0]-mean))
            if dist < minDist:
                minDist = dist
                colorId = i
 
        return colorId

if __name__ == '__main__':
    rospy.init_node('object_tracker')
    ArUcoTracker()
    rospy.spin()
