import sys, math
import numpy as np
from pdb import set_trace
from copy import copy

import Box2D
from Box2D.b2 import (edgeShape, circleShape, fixtureDef, polygonShape, revoluteJointDef, contactListener)

import gym
from gym import spaces
from gym.envs.box2d.car_dynamics import Car
from gym.utils import colorize, seeding, EzPickle

import pyglet
from pyglet import gl

# Easiest continuous control task to learn from pixels, a top-down racing environment.
# Discreet control is reasonable in this environment as well, on/off discretisation is
# fine.
#
# State consists of STATE_W x STATE_H pixels.
#
# Reward is -0.1 every frame and +1000/N for every track tile visited, where N is
# the total number of tiles in track. For example, if you have finished in 732 frames,
# your reward is 1000 - 0.1*732 = 926.8 points.
#
# Game is solved when agent consistently gets 900+ points. Track is random every episode.
#
# Episode finishes when all tiles are visited. Car also can go outside of PLAYFIELD, that
# is far off the track, then it will get -100 and die.
#
# Some indicators shown at the bottom of the window and the state RGB buffer. From
# left to right: true speed, four ABS sensors, steering wheel position, gyroscope.
#
# To play yourself (it's rather fast for humans), type:
#
# python gym/envs/box2d/car_racing.py
#
# Remember it's powerful rear-wheel drive car, don't press accelerator and turn at the
# same time.
#
# Created by Oleg Klimov. Licensed on the same terms as the rest of OpenAI Gym.

STATE_W = 96   # less than Atari 160x192
STATE_H = 96
VIDEO_W = 600
VIDEO_H = 400
WINDOW_H = 700
WINDOW_W = int(WINDOW_H*1.5)

SCALE       = 6.0        # Track scale
TRACK_RAD   = 900/SCALE  # Track is heavily morphed circle with this radius
PLAYFIELD   = 2000/SCALE # Game over boundary
FPS         = 50
ZOOM        = 2.7        # Camera zoom, 0.25 to take screenshots, default 2.7
ZOOM_FOLLOW = True       # Set to False for fixed view (don't use zoom)

TRACK_DETAIL_STEP = 21/SCALE
TRACK_TURN_RATE = 0.31
TRACK_WIDTH = 40/SCALE
BORDER = 8/SCALE
BORDER_MIN_COUNT = 4

ROAD_COLOR = [0.4, 0.4, 0.4]

# Debug actions
SHOW_ENDS_OF_TRACKS       = False   # Shows with red dots the end of track
SHOW_INTERSECTIONS_POINTS = False   # Shows with green dots the intersections of main track
SHOW_AXIS                 = False   # Draws two lines where the x and y axis are
ZOOM_OUT                  = 0       # Shows maps in general and does not do zoom
if ZOOM_OUT: ZOOM         = 0.25    # Complementary to ZOOM_OUT

class FrictionDetector(contactListener):
    def __init__(self, env):
        contactListener.__init__(self)
        self.env = env
    def BeginContact(self, contact):
        self._contact(contact, True)
    def EndContact(self, contact):
        self._contact(contact, False)
    def _contact(self, contact, begin):
        tile = None
        obj = None
        u1 = contact.fixtureA.body.userData
        u2 = contact.fixtureB.body.userData
        if u1 and "road_friction" in u1.__dict__:
            tile = u1
            obj  = u2
        if u2 and "road_friction" in u2.__dict__:
            tile = u2
            obj  = u1
        if not tile: return

        tile.color[0] = ROAD_COLOR[0]
        tile.color[1] = ROAD_COLOR[1]
        tile.color[2] = ROAD_COLOR[2]
        if not obj or "tiles" not in obj.__dict__: return
        if begin:
            obj.tiles.add(tile)
            #print tile.road_friction, "ADD", len(obj.tiles)
            if not tile.road_visited:
                tile.road_visited = True
                self.env.reward += 1000.0/len(self.env.track)
                self.env.tile_visited_count += 1
        else:
            obj.tiles.remove(tile)
            #print tile.road_friction, "DEL", len(obj.tiles) -- should delete to zero when on grass (this works)

class CarRacing(gym.Env, EzPickle):
    metadata = {
        'render.modes': ['human', 'rgb_array', 'state_pixels'],
        'video.frames_per_second' : FPS
    }

    def set_velocity(self, velocity=[0.0,0.0]):
        self.car.hull.linearVelocity.Set(velocity[0],velocity[1])

    def set_speed(self, speed):
        ang = self.car.hull.angle + math.pi/2
        velocity_x = math.cos(ang)*speed
        velocity_y = math.sin(ang)*speed
        self.set_velocity([velocity_x, velocity_y])

    def __init__(self):
        EzPickle.__init__(self)
        self.seed()
        self.contactListener_keepref = FrictionDetector(self)
        self.world = Box2D.b2World((0,0), contactListener=self.contactListener_keepref)
        self.viewer = None
        self.invisible_state_window = None
        self.invisible_video_window = None
        self.road = None
        self.car = None
        self.reward = 0.0
        self.prev_reward = 0.0

        # Config
        self.num_lanes_changes = 3   # Number of points where lanes change from 1 lane to two and viceversa 
        self.num_tracks        = 2   # Number of tracks, this control the complexity of the map
        self.num_lanes         = 2   # Number of lanes, 1 or 2
        self.prob_obstacle     = 0.1 # Percentage of finding a obstacle in a point of the row

        self.action_space = spaces.Box( np.array([-1,0,0]), np.array([+1,+1,+1]), dtype=np.float32)  # steer, gas, brake
        self.observation_space = spaces.Box(low=0, high=255, shape=(STATE_H, STATE_W, 3), dtype=np.uint8)

    def set_config(self, num_tracks=2, num_lanes=2, num_lanes_changes=2, prob_obstacle=0.1):
        '''
        Controls some attributes of the game, such as the number of tracks (num_tracks)
        which is a proxy to control the complexity of map, the number of lanes (num_lanes_changes) and
        the probability of finding an obstacle (prob_osbtacle).
        Only call this method onceto set the parameters

        num_tracks:       (int 2)    Number of tracks, in {1,2}, 1: simple, 2: complex
        num_lanes:        (int 2)    Number of lanes in track, > 0 ({1,2})
        num_lanes_changes (int 2)    Number of changes from 2 to 1 or viceversa, this 
                                     is ultimately transform as a probability over the
                                     total number of points in track
        prob_obstacle     (foat 0.1) The probability of finding an obstacle a point of 
                                     the track, [0,1]
        '''
        self.num_lanes         = num_lanes  if num_lanes  > 0 and num_lanes  <= 2 else self.num_lanes
        self.num_tracks        = num_tracks if num_tracks > 0 and num_tracks <= 2 else self.num_tracks
        self.prob_obstacle     = prob_obstacle if prob_obstacle >= 0 and prob_obstacle <= 1 else self.prob_obstacle
        self.num_lanes_changes = num_lanes_changes if num_lanes_changes >= 0 else self.num_lanes_changes

    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _destroy(self):
        if not self.road: return
        for t in self.road:
            self.world.DestroyBody(t)
        self.road = []
        self.car.destroy()

    def place_agent(self, position):
        self.car.destroy()
        self.car = Car(self.world, *position)

    def _get_track(self, CHECKPOINTS, TRACK_RAD=900/SCALE):

        CHECKPOINTS = 12

        # Create checkpoints
        checkpoints = []
        for c in range(CHECKPOINTS):
            alpha = 2*math.pi*c/CHECKPOINTS + self.np_random.uniform(0, 2*math.pi*1/CHECKPOINTS)
            rad = self.np_random.uniform(TRACK_RAD/3, TRACK_RAD)
            if c==0:
                alpha = 0
                rad = 1.5*TRACK_RAD
            if c==CHECKPOINTS-1:
                alpha = 2*math.pi*c/CHECKPOINTS
                self.start_alpha = 2*math.pi*(-0.5)/CHECKPOINTS
                rad = 1.5*TRACK_RAD
            checkpoints.append( (alpha, rad*math.cos(alpha), rad*math.sin(alpha)) )

        #print "\n".join(str(h) for h in checkpoints)
        #self.road_poly = [ (    # uncomment this to see checkpoints
        #    [ (tx,ty) for a,tx,ty in checkpoints ],
        #    (0.7,0.7,0.9) ) ]
        self.road = []

        # Go from one checkpoint to another to create track
        x, y, beta = 1.5*TRACK_RAD, 0, 0
        dest_i = 0
        laps = 0
        track = []
        no_freeze = 2500
        visited_other_side = False
        while 1:
            alpha = math.atan2(y, x)
            if visited_other_side and alpha > 0:
                laps += 1
                visited_other_side = False
            if alpha < 0:
                visited_other_side = True
                alpha += 2*math.pi
            while True: # Find destination from checkpoints
                failed = True
                while True:
                    dest_alpha, dest_x, dest_y = checkpoints[dest_i % len(checkpoints)]
                    if alpha <= dest_alpha:
                        failed = False
                        break
                    dest_i += 1
                    if dest_i % len(checkpoints) == 0: break
                if not failed: break
                alpha -= 2*math.pi
                continue
            r1x = math.cos(beta)
            r1y = math.sin(beta)
            p1x = -r1y
            p1y = r1x
            dest_dx = dest_x - x  # vector towards destination
            dest_dy = dest_y - y
            proj = r1x*dest_dx + r1y*dest_dy  # destination vector projected on rad
            while beta - alpha >  1.5*math.pi: beta -= 2*math.pi
            while beta - alpha < -1.5*math.pi: beta += 2*math.pi
            prev_beta = beta
            proj *= SCALE
            if proj >  0.3: beta -= min(TRACK_TURN_RATE, abs(0.001*proj))
            if proj < -0.3: beta += min(TRACK_TURN_RATE, abs(0.001*proj))
            x += p1x*TRACK_DETAIL_STEP
            y += p1y*TRACK_DETAIL_STEP
            track.append( (alpha,prev_beta*0.5 + beta*0.5,x,y) )
            if laps > 4: break
            no_freeze -= 1
            if no_freeze==0: break
        #print "\n".join([str(t) for t in enumerate(track)])

        # Find closed loop range i1..i2, first loop should be ignored, second is OK
        i1, i2 = -1, -1
        i = len(track)
        while True:
            i -= 1
            if i==0: return False  # Failed
            pass_through_start = track[i][0] > self.start_alpha and track[i-1][0] <= self.start_alpha
            if pass_through_start and i2==-1:
                i2 = i
            elif pass_through_start and i1==-1:
                i1 = i
                break
        print("Track generation: %i..%i -> %i-tiles track" % (i1, i2, i2-i1))
        assert i1!=-1
        assert i2!=-1

        track = track[i1:i2-1]

        first_beta = track[0][1]
        first_perp_x = math.cos(first_beta)
        first_perp_y = math.sin(first_beta)
        # Length of perpendicular jump to put together head and tail
        well_glued_together = np.sqrt(
            np.square( first_perp_x*(track[0][2] - track[-1][2]) ) +
            np.square( first_perp_y*(track[0][3] - track[-1][3]) ))
        if well_glued_together > TRACK_DETAIL_STEP:
            return False

        track     = [[track[i-1],track[i]] for i in range(len(track))]
        return track

    def _create_info(self):
        '''
        Creates the matrix with the information about the track points,
        whether they are at the end of the track, if they are intersections
        '''
        # Get if point is at the end
        info  = np.zeros((sum(len(t) for t in self.tracks)),dtype=[
            ('track', 'int'),
            ('end','bool'),
            ('begining', 'bool'),
            ('intersection', 'bool'),
            ('start','bool'),
            ('lanes',np.ndarray),
            ('obstacles',np.ndarray)])

        #info['lanes'] = copy([[True]*self.num_lanes])*len(info)
        for i in range(len(info)):
            info[i]['lanes'] = [True, True]

        for i in range(1, len(self.tracks)): 
            track = self.tracks[i]
            info[len(self.tracks[i-1])-1:len(self.tracks[i-1])+len(track)]['track'] = i
            for j in range(len(track)):
                pos = j + len(self.tracks[i-1])
                p = track[j]
                next_p = track[(j+1)%len(track)]
                last_p = track[j-1]
                if np.array_equal(p[1], next_p[0]) == False:
                    # it is at the end
                    info[pos]['end'] = True
                elif np.array_equal(p[0], last_p[1]) == False:
                    # it is at the start
                    info[pos]['start'] = True

        # Find if tiles in principal track are close to an intersection TODO NOT WORKING
        intersections_idx = set()
        for point in self.track[np.logical_or(info['end'],info['start'])][:,1,2:]:
            intersections_idx.add(np.argmin(np.linalg.norm(self.tracks[0][:,1,2:] - point, axis=1)))
        info['intersection'][list(intersections_idx)] = True

        self.info = info

    def _set_lanes(self):
        if self.num_lanes_changes > 0 and self.num_lanes > 1:
            rm_lane = 0 # 1 remove lane, 0 keep lane
            lane    = 0 # Which lane will be removed
            changes = self.np_random.randint(0,len(self.track),self.num_lanes_changes)
            for i, point in enumerate(self.track):
                change = True if i in changes else False
                rm_lane = (rm_lane+change)%2

                if change and rm_lane == 1: # if it is time to change and the turn is to remove lane
                    lane = np.random.randint(0,2,1)[0]

                if rm_lane:
                    self.info[i]['lanes'][lane] = False

                # Change if end/inter of or if change prob
                if self.info[i]['end'] or self.info[i]['start']: 
                    rm_lane = 0
        
    def _create_track(self):

        tracks = []
        for _ in range(self.num_tracks):
            track = self._get_track(12)
            if not track: return track
            tracks.append(track)

        self.tracks = tracks
        self._remove_roads()

        self.track  = np.concatenate(self.tracks)

        self._create_info()
        self._set_lanes()
    
        # Red-white border on hard turns
        borders = []
        for track in self.tracks:
            border = [False]*len(track)
            for i in range(1,len(track)):
                good = True
                oneside = 0
                for neg in range(BORDER_MIN_COUNT):
                    beta1 = track[i-neg][1][1]
                    beta2 = track[i-neg][0][1]
                    good &= abs(beta1 - beta2) > TRACK_TURN_RATE*0.2
                    oneside += np.sign(beta1 - beta2)
                good &= abs(oneside) == BORDER_MIN_COUNT
                border[i] = good
            for i in range(len(track)):
                for neg in range(BORDER_MIN_COUNT):
                    border[i-neg] |= border[i]
            borders.append(border)
                
        # Creating borders for printing
        pos = 0
        for j in range(self.num_tracks):
            track  = self.tracks[j]
            border = borders[j]
            for i in range(len(track)):
                alpha1, beta1, x1, y1 = track[i][1]
                alpha2, beta2, x2, y2 = track[i][0]
                if border[i]:
                    side = np.sign(beta2 - beta1)

                    c = 1

                    if self.num_lanes > 1:
                        if side == -1 and self.info[pos]['lanes'][0] == False: c = 0
                        if side == +1 and self.info[pos]['lanes'][1] == False: c = 0

                    b1_l = (x1 + side* TRACK_WIDTH*c        *math.cos(beta1), y1 + side* TRACK_WIDTH*c        *math.sin(beta1))
                    b1_r = (x1 + side*(TRACK_WIDTH*c+BORDER)*math.cos(beta1), y1 + side*(TRACK_WIDTH*c+BORDER)*math.sin(beta1))
                    b2_l = (x2 + side* TRACK_WIDTH*c        *math.cos(beta2), y2 + side* TRACK_WIDTH*c        *math.sin(beta2))
                    b2_r = (x2 + side*(TRACK_WIDTH*c+BORDER)*math.cos(beta2), y2 + side*(TRACK_WIDTH*c+BORDER)*math.sin(beta2))
                    self.road_poly.append(( [b1_l, b1_r, b2_r, b2_l], (1,1,1) if i%2==0 else (1,0,0) ))
                pos += 1


        # Create tiles
        p3 = [] # in order to save all points 3 to create joints
        for j in range(len(self.track)):
            obstacle = np.random.binomial(1,self.prob_obstacle)
            alpha1, beta1, x1, y1 = self.track[j][1]
            alpha2, beta2, x2, y2 = self.track[j][0]

            for lane in range(self.num_lanes):
                if self.info[j]['lanes'][lane]:

                    r = 1- ((lane+1)%self.num_lanes)
                    l = 1- ((lane+2)%self.num_lanes)

                    # Get if it is the first or last
                    first = False # first of lane
                    last  = False # last tile of line

                    if self.info[j]['end'] == False and self.info[j]['start'] == False:

                        # Getting if first tile of lane
                        # if last tile was from the same lane
                        if self.info[j-1]['track'] == self.info[j]['track']:
                            # If last tile didnt exist
                            if self.info[j-1]['lanes'][lane] == False:
                                first = True
                        elif np.where(self.info['track'] == self.info[j]['track'])[0].min() == j: # if it is first tile of track
                            #check the last of their track
                            if self.info[self.info['track'] == self.info[j]['track']][-1]['lanes'][lane] == False:
                                first = True
                        # if next tile is from the same lane
                        if self.info[(j+1)%len(self.info)]['track'] == self.info[j]['track']:
                            # If last tile didnt exist
                            if self.info[(j+1)%len(self.info)]['lanes'][lane] == False:
                                last = True
                        elif np.where(self.info['track'] == self.info[j]['track'])[0].max() == j: # if it is last tile of track
                            #check the last of their track
                            if self.info[self.info['track'] == self.info[j]['track']][0]['lanes'][lane] == False:
                                last = True

                        road1_l = (x1 - (1-last)*l*TRACK_WIDTH*math.cos(beta1), y1 - (1-last)*l*TRACK_WIDTH*math.sin(beta1))
                        road1_r = (x1 + (1-last)*r*TRACK_WIDTH*math.cos(beta1), y1 + (1-last)*r*TRACK_WIDTH*math.sin(beta1))
                        road2_l = (x2 - (1-first)*l*TRACK_WIDTH*math.cos(beta2), y2 - (1-first)*l*TRACK_WIDTH*math.sin(beta2))
                        road2_r = (x2 + (1-first)*r*TRACK_WIDTH*math.cos(beta2), y2 + (1-first)*r*TRACK_WIDTH*math.sin(beta2))

                    elif False: # if it is end or start
                        
                        road1_l = (x1 - (1-last)*l*TRACK_WIDTH*math.cos(beta1), y1 - (1-last)*l*TRACK_WIDTH*math.sin(beta1)) # The first points will be the same
                        road1_r = (x1 + (1-last)*r*TRACK_WIDTH*math.cos(beta1), y1 + (1-last)*r*TRACK_WIDTH*math.sin(beta1)) # The first points will be the same

                        # Get the closest point to a line make by the continuing trend of the original road points, the points will be the points 
                        # under a radius r from line to avoid taking points far away in the other extreme of the track
                        # Remember the distance from a point p3 to a line p1,p2 is d = norm(np.cross(p2-p1, p1-p3))/norm(p2-p1)
                        # p1=(x1,y1)+sin/cos, p2=(x2,y2)+sin/cos, p3=points in poly
                        if self.info[j]['end']:
                            p1 = road1_l
                            p2 = road2_l
                        else:
                            p1 = 2
                            p2 = 2

                        max_idx = len(self.tracks[0]) # this will work because only seconday tracks have ends
                        if len(p3) == 0:
                            p3 = sum([x[0] for x in self.road_poly[:max_idx]],[])

                            # filter p3 by distance to p1 < TRACK_WIDTH*2

                        set_trace() # Check if d is correctly contructed
                        d = np.linalg.norm(np.cross(p2-p1,p1-p3))/np.linalg.norm(p2-p1)


                        road2_l = (x2 - (1-first)*l*TRACK_WIDTH*math.cos(beta2), y2 - (1-first)*l*TRACK_WIDTH*math.sin(beta2)) # Depends on closest point
                        road2_r = (x2 + (1-first)*r*TRACK_WIDTH*math.cos(beta2), y2 + (1-first)*r*TRACK_WIDTH*math.sin(beta2)) # Depends on closest point


                    t = self.world.CreateStaticBody( fixtures = fixtureDef(
                        shape=polygonShape(vertices=[road1_l, road1_r, road2_r, road2_l])
                        ))
                    t.userData = t
                    t.obstacle = obstacle
                    c = 0.01*(i%3)
                    t.color = [ROAD_COLOR[0], ROAD_COLOR[1], ROAD_COLOR[2]]
                    t.road_visited = False
                    t.road_friction = 1.0
                    t.fixtures[0].sensor = True
                    self.road_poly.append(( [road1_l, road1_r, road2_r, road2_l], t.color ))
                    self.road.append(t)

                    # Adding obstacles
                    if obstacle == 1:
                        x = (x1+x2)/2
                        y = (y1+y2)/2 + TRACK_WIDTH*math.sin(beta1)/2
                        l1 = (x+0.1, y+0.4)
                        r1 = (x-0.1, y+0.4)
                        l2 = (x-0.4, y-0.4)
                        r2 = (x+0.4, y-0.4)
                        color = [1,0.5,0.3]
                        self.road_poly.append(( [l1,r1,l2,r2], color ))
                        l1 = (x+0.5, y-0.4)
                        r1 = (x-0.5, y-0.4)
                        l2 = (x-0.5, y-0.6)
                        r2 = (x+0.5, y-0.6)
                        color = [1,0.5,0.3]
                        self.road_poly.append(( [l1,r1,l2,r2], color ))

        return True

    def reset(self):
        '''
        car_position [angle float, x float, y float]
                     Position of the car
                     Default: first tile of principal track
        '''
        self._destroy()
        self.reward = 0.0
        self.prev_reward = 0.0
        self.tile_visited_count = 0
        self.t = 0.0
        self.road_poly = []
        self.track = []
        self.tracks = []
        self.human_render = False

        while True:
            success = self._create_track()
            if success: break
            print("retry to generate track (normal if there are not many of this messages)")
        #if car_position is None or car_position[0] == None or car_position[1] == None or car_position[2] == None:
        car_position = self.track[0][1][1:4]
        self.car = Car(self.world, *car_position)
        self.place_agent(self.get_rnd_point_in_track())

        return self.step(None)[0]

    def step(self, action):
        if action is not None:
            self.car.steer(-action[0])
            self.car.gas(action[1])
            self.car.brake(action[2])

        self.car.step(1.0/FPS)
        self.world.Step(1.0/FPS, 6*30, 2*30)
        self.t += 1.0/FPS

        self.state = self.render("state_pixels")

        step_reward = 0
        done = False
        if action is not None: # First step without action, called from reset()
            self.reward -= 0.1
            # We actually don't want to count fuel spent, we want car to be faster.
            #self.reward -=  10 * self.car.fuel_spent / ENGINE_POWER
            self.car.fuel_spent = 0.0
            step_reward = self.reward - self.prev_reward
            self.prev_reward = self.reward
            if self.tile_visited_count==len(self.track):
                done = True
            x, y = self.car.hull.position
            if abs(x) > PLAYFIELD or abs(y) > PLAYFIELD:
                done = True
                step_reward = -100

        return self.state, step_reward, done, {}

    def render(self, mode='human'):
        if self.viewer is None:
            from gym.envs.classic_control import rendering
            self.viewer = rendering.Viewer(WINDOW_W, WINDOW_H)
            self.score_label = pyglet.text.Label('0000', font_size=36,
                x=20, y=WINDOW_H*2.5/40.00, anchor_x='left', anchor_y='center',
                color=(255,255,255,255))
            self.transform = rendering.Transform()

        if "t" not in self.__dict__: return  # reset() not called yet

        zoom = 0.1*SCALE*max(1-self.t, 0) + ZOOM*SCALE*min(self.t, 1)   # Animate zoom first second
        zoom_state  = ZOOM*SCALE*STATE_W/WINDOW_W
        zoom_video  = ZOOM*SCALE*VIDEO_W/WINDOW_W
        scroll_x = self.car.hull.position[0]
        scroll_y = self.car.hull.position[1]
        angle = -self.car.hull.angle
        vel = self.car.hull.linearVelocity
        if np.linalg.norm(vel) > 0.5:
            angle = math.atan2(vel[0], vel[1])
        self.transform.set_scale(zoom, zoom)
        # TODO to screenshots read comments below
        if ZOOM_OUT:
            self.transform.set_translation(WINDOW_W/2, WINDOW_H/2)
            #self.transform.set_rotation(angle) # get screenshots commet this out
        else:
            self.transform.set_translation(
                # to get nice screenshots use WINDOW_X/2
                WINDOW_W/2 - (scroll_x*zoom*math.cos(angle) - scroll_y*zoom*math.sin(angle)), 
                WINDOW_H/4 - (scroll_x*zoom*math.sin(angle) + scroll_y*zoom*math.cos(angle)) )
            self.transform.set_rotation(angle) # get screenshots commet this out

        self.car.draw(self.viewer, mode!="state_pixels")

        arr = None
        win = self.viewer.window
        if mode != 'state_pixels':
            win.switch_to()
            win.dispatch_events()
        if mode=="rgb_array" or mode=="state_pixels":
            win.clear()
            t = self.transform
            if mode=='rgb_array':
                VP_W = VIDEO_W
                VP_H = VIDEO_H
            else:
                VP_W = STATE_W
                VP_H = STATE_H
            gl.glViewport(0, 0, VP_W, VP_H)
            t.enable()
            self.render_road()
            self.render_road_lines()
            for geom in self.viewer.onetime_geoms:
                geom.render()
            t.disable()
            self.render_indicators(WINDOW_W, WINDOW_H)  # TODO: find why 2x needed, wtf
            image_data = pyglet.image.get_buffer_manager().get_color_buffer().get_image_data()
            arr = np.fromstring(image_data.data, dtype=np.uint8, sep='')
            arr = arr.reshape(VP_H, VP_W, 4)
            arr = arr[::-1, :, 0:3]

        if mode=="rgb_array" and not self.human_render: # agent can call or not call env.render() itself when recording video.
            win.flip()

        if mode=='human':
            self.human_render = True
            win.clear()
            t = self.transform
            gl.glViewport(0, 0, WINDOW_W, WINDOW_H)
            t.enable()
            self.render_road()
            self.render_road_lines()
            for geom in self.viewer.onetime_geoms:
                geom.render()
            t.disable()
            self.render_indicators(WINDOW_W, WINDOW_H)
            win.flip()

        self.viewer.onetime_geoms = []
        return arr

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None

    def _remove_roads(self):

        if self.num_tracks > 1:
            def _get_section(first,last,track):
                sec = []
                pos = 0
                found = False
                while 1:
                    point = track[pos%track.shape[0],:,2:]
                    if np.linalg.norm(point[1]-first) <= TRACK_WIDTH/2:
                        found = True
                    if found:
                        sec.append(point)
                        if np.linalg.norm(point[1]-last) <= TRACK_WIDTH/2:
                            break
                    pos = pos+1
                    if pos / track.shape[0] >= 2: break
                if sec == []: return False
                return np.array(sec)

            THRESHOLD = TRACK_WIDTH*2

            track1 = np.array(self.tracks[0])
            track2 = np.array(self.tracks[1])

            points1 = track1[:,:,[2,3]]
            points2 = track2[:,:,[2,3]]

            #inter1 = np.array([x for x in points2 if (np.linalg.norm(points1[:,1,:]-x[1:], axis=1) <= TRACK_WIDTH*1.25).sum() >= 1]) TODO delete
            inter2 = np.array([x for x in points2 if (np.linalg.norm(points1[:,1,:]-x[1:], axis=1) <= TRACK_WIDTH/3.5 ).sum() >= 1])

            intersections = []
            for i in range(inter2.shape[0]):
                if np.array_equal(inter2[i-1,1,:],inter2[i,0,:]) == False or np.array_equal(inter2[i,1,:], inter2[((i+1)%len(inter2)),0,:]) == False:
                    intersections.append(inter2[i])
            intersections = np.array(intersections)

            # For each point in intersection
            # > get section of both roads
            # > For each point in section in second road
            # > > get min distance
            # > get max of distances
            # if max dist < threshold remove
            removed_idx = set()
            for i in range(intersections.shape[0]):
                _, first = intersections[i-1]
                last,_ = intersections[i]

                sec1 = _get_section(first,last,track1)
                sec2 = _get_section(first,last,track2)
                
                if sec1 is not False and sec2 is not False:
                    max_min_d = 0
                    remove = False
                    for point in sec1[:,1]:
                        dist = np.linalg.norm(sec2[:,1] - point, axis=1).min()
                        max_min_d = dist if max_min_d < dist else max_min_d
                    if max_min_d < THRESHOLD*2: remove = True
                    
                    # Removing tiles
                    if remove:
                        for point in sec2:
                            idx = np.all(track2[:,:,[2,3]] == point, axis=(1,2))
                            removed_idx.update(np.where(idx)[0])

            track2 = np.delete(track2, list(removed_idx), axis=0) # efficient way to delete them from np.array

            self.intersections = intersections
            
            self.tracks[0] = track1
            self.tracks[1] = track2


    def _render_tiles(self):
        '''
        Can only be called inside a glBegin
        '''
        # drawing road old way
        for poly, color in self.road_poly:
            gl.glColor4f(color[0], color[1], color[2], 1)
            for p in poly:
                gl.glVertex3f(p[0], p[1], 0)

    def render_road(self):
        gl.glBegin(gl.GL_QUADS)
        gl.glColor4f(0.4, 0.8, 0.4, 1.0)
        gl.glVertex3f(-PLAYFIELD, +PLAYFIELD, 0)
        gl.glVertex3f(+PLAYFIELD, +PLAYFIELD, 0)
        gl.glVertex3f(+PLAYFIELD, -PLAYFIELD, 0)
        gl.glVertex3f(-PLAYFIELD, -PLAYFIELD, 0)
        gl.glColor4f(0.4, 0.9, 0.4, 1.0)
        k = PLAYFIELD/20.0
        for x in range(-20, 20, 2):
            for y in range(-20, 20, 2):
                gl.glVertex3f(k*x + k, k*y + 0, 0)
                gl.glVertex3f(k*x + 0, k*y + 0, 0)
                gl.glVertex3f(k*x + 0, k*y + k, 0)
                gl.glVertex3f(k*x + k, k*y + k, 0)

        self._render_tiles()

        # drawing angles of old config, the 
        # black line is the angle (NOT WORKING)
        for track in []:#self.tracks:
            for point1, point2 in track:
                alpha1,beta1,x1,y1 = point1
                beta1 = alpha1

                gl.glColor4f(0, 0, 0, 0)
                gl.glVertex3f(x1+2,y1, 0)
                gl.glVertex3f(x1+2+math.cos(beta1)*2,y1+math.sin(beta1)*2, 0)
                gl.glVertex3f(x1-2+math.cos(beta1)*2,y1+math.sin(beta1)*2, 0)
                gl.glVertex3f(x1-2,y1, 0)

        # Ploting axis
        if SHOW_AXIS:
            # x-axis
            gl.glColor4f(0, 0, 0, 1)
            gl.glVertex3f(-PLAYFIELD, 2, 0)
            gl.glVertex3f(+PLAYFIELD, 2, 0)
            gl.glVertex3f(+PLAYFIELD,-2, 0)
            gl.glVertex3f(-PLAYFIELD,-2, 0)
            
            # y-axis
            gl.glVertex3f(+2,-PLAYFIELD, 0)
            gl.glVertex3f(+2,+PLAYFIELD, 0)
            gl.glVertex3f(-2,+PLAYFIELD, 0)
            gl.glVertex3f(-2,-PLAYFIELD, 0)
        
        gl.glEnd()

    def render_road_lines(self):
        pass        

    def render_debug_clues(self):
        gl.glBegin(gl.GL_QUADS)

        if SHOW_ENDS_OF_TRACKS:
            for x,y in self.track[np.logical_or(self.info['end'],self.info['start'])][:,1,2:]:
                gl.glColor4f(0, 1, 0, 1)
                gl.glVertex3f(x+2,y+2,0)
                gl.glVertex3f(x-2,y+2,0)
                gl.glVertex3f(x-2,y-2,0)
                gl.glVertex3f(x+2,y-2,0)
            
        if SHOW_INTERSECTIONS_POINTS:
            for x,y in self.track[self.info['intersection']][:,1,2:]:
                gl.glColor4f(1, 0, 0, 1)
                gl.glVertex3f(x+1,y+1,0)
                gl.glVertex3f(x-1,y+1,0)
                gl.glVertex3f(x-1,y-1,0)
                gl.glVertex3f(x+1,y-1,0)
        gl.glEnd()


    def render_indicators(self, W, H):
        gl.glBegin(gl.GL_QUADS)
        s = W/40.0
        h = H/40.0
        gl.glColor4f(0,0,0,1)
        gl.glVertex3f(W, 0, 0)
        gl.glVertex3f(W, 5*h, 0)
        gl.glVertex3f(0, 5*h, 0)
        gl.glVertex3f(0, 0, 0)
        def vertical_ind(place, val, color):
            gl.glColor4f(color[0], color[1], color[2], 1)
            gl.glVertex3f((place+0)*s, h + h*val, 0)
            gl.glVertex3f((place+1)*s, h + h*val, 0)
            gl.glVertex3f((place+1)*s, h, 0)
            gl.glVertex3f((place+0)*s, h, 0)
        def horiz_ind(place, val, color):
            gl.glColor4f(color[0], color[1], color[2], 1)
            gl.glVertex3f((place+0)*s, 4*h , 0)
            gl.glVertex3f((place+val)*s, 4*h, 0)
            gl.glVertex3f((place+val)*s, 2*h, 0)
            gl.glVertex3f((place+0)*s, 2*h, 0)
        true_speed = np.sqrt(np.square(self.car.hull.linearVelocity[0]) + np.square(self.car.hull.linearVelocity[1]))
        vertical_ind(5, 0.02*true_speed, (1,1,1))
        vertical_ind(7, 0.01*self.car.wheels[0].omega, (0.0,0,1)) # ABS sensors
        vertical_ind(8, 0.01*self.car.wheels[1].omega, (0.0,0,1))
        vertical_ind(9, 0.01*self.car.wheels[2].omega, (0.2,0,1))
        vertical_ind(10,0.01*self.car.wheels[3].omega, (0.2,0,1))
        horiz_ind(20, -10.0*self.car.wheels[0].joint.angle, (0,1,0))
        horiz_ind(30, -0.8*self.car.hull.angularVelocity, (1,0,0))
        gl.glEnd()
        self.score_label.text = "%04i" % self.reward
        self.score_label.draw()

    def get_rnd_point_in_track(self,border=True):
        '''
        returns a random point in the track with the angle equal 
        to the tile of the track, the x position can be randomly 
        in the x (relative) axis of the tile, border=True make 
        sure the x position is enough to make the car fit in 
        the track, otherwise the point can be in the extreme 
        of the track and two wheels will be outside the track
        -----
        Returns: [beta, x, y]
        '''
        idx = self.np_random.randint(0, len(self.track))
        alpha, beta, x, y = self.track[idx,1,:]
        r,l = self.info[idx]['lanes']
        x_from = -TRACK_WIDTH*l+math.cos(alpha)*border*TRACK_WIDTH/3.5
        x_to   = +TRACK_WIDTH*r-math.sin(alpha)*border*TRACK_WIDTH/3.5
        x += np.random.uniform(x_from,x_to) 
        return [beta, x, y]


if __name__=="__main__":
    from pyglet.window import key
    a = np.array( [0.0, 0.0, 0.0] )
    def key_press(k, mod):
        global restart
        if k==0xff0d: restart = True
        if k==key.LEFT:  a[0] = -1.0
        if k==key.RIGHT: a[0] = +1.0
        if k==key.UP:    a[1] = +1.0
        if k==key.DOWN:  a[2] = +0.8   # set 1.0 for wheels to block to zero rotation
    def key_release(k, mod):
        if k==key.LEFT  and a[0]==-1.0: a[0] = 0
        if k==key.RIGHT and a[0]==+1.0: a[0] = 0
        if k==key.UP:    a[1] = 0
        if k==key.DOWN:  a[2] = 0
    env = CarRacing()
    env.render()
    record_video = False
    if record_video:
        env.monitor.start('/tmp/video-test', force=True)
    env.viewer.window.on_key_press = key_press
    env.viewer.window.on_key_release = key_release
    while True:
        env.reset()
        total_reward = 0.0
        steps = 0
        restart = False
        while True:
            s, r, done, info = env.step(a)
            total_reward += r
            if steps % 200 == 0 or done:
                print("\naction " + str(["{:+0.2f}".format(x) for x in a]))
                print("step {} total_reward {:+0.2f}".format(steps, total_reward))
                #import matplotlib.pyplot as plt
                #plt.imshow(s)
                #plt.savefig("test.jpeg")
            steps += 1
            if not record_video: # Faster, but you can as well call env.render() every time to play full window.
                env.render()
            if done or restart: break
    env.close()
