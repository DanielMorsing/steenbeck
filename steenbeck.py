#!/usr/bin/env python

"""
Start of the steenbeck project

"""
import subprocess
import json
import pprint
import argparse
from python_get_resolve import GetResolve

def FindTimeline(project):
    cnt = project.GetTimelineCount()
    for i in range(1, cnt+1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == args.t:
            return tl
        
    raise Exception("Could not find timeline {}".format(args.t))


def GetTimelines( resolve ):
    projectManager = resolve.GetProjectManager()
    project = projectManager.GetCurrentProject()
    fromTl = FindTimeline(project)
    toTl = project.GetCurrentTimeline()
    return fromTl, toTl

def FindKeyframe(frames):
    for i in frames:
        if i["flags"] == "K__":
            return i
    
    raise Exception("could not find keyframe")

def PtsToFramenum(timebase, framerate, pts):
    # ffmpeg doesn't support seeking by frame number, we only get presentation
    # timestamps.
    # davinci requires frame numbers to set the in and out points for renders.
    # Luckily, we know that resolve doesn't support variable framerate timelines
    # and that this file came out of resolve
    return (pts/timebase)*framerate

parser = argparse.ArgumentParser()
parser.add_argument('-t')
parser.add_argument('-f')
parser.add_argument('-o')

args = parser.parse_args()

resolve = GetResolve()

fromTimeline, toTimeline = GetTimelines(resolve)

if fromTimeline.GetStartFrame() != toTimeline.GetStartFrame():
    raise Exception("differing start frames")


# run through every frame of the video, creating a hash of the current frame
# properties. This is horribly inefficient, but the alternative
# is to actually do the maths for insertion and doing "data structures"
# and "computer science". Life's too short
# TODO(dmo): this is special cased to only handle one track right now
fromitems = fromTimeline.GetItemListInTrack('video', 1)
toitems = toTimeline.GetItemListInTrack('video', 1)

fromFrames = []

for i in fromitems:
    # TODO(dmo): when we make this work for multiple tracks, this
    # obviously has to change. It is not guaranteed to be contiguous over
    # every frame
    # also, we need to look at file properties. this will especially become
    # important when we have to work with titles, which will change text
    n = i.GetName()
    s = i.GetStart()
    e = i.GetEnd()
    for r in range(s,e):
        m = (n,r)
        fromFrames.append(hash(m))


command = [
    "./ffprobe.exe",
    "-print_format", "json",
    "-select_streams", "v:0",
    "-show_streams",
    "-show_packets",
    "-read_intervals", "00:04.99%+#100", # TODO(dmo): replace with format
    "-i", args.f
]
res = subprocess.run(command, capture_output=True)
ffprobeoutput = json.loads(res.stdout)
gopentry = ffprobeoutput["packets"][0]
gopexit = FindKeyframe(ffprobeoutput["packets"][1:])
stream = ffprobeoutput["streams"][0]

d, q = stream["time_base"].split('/')
if int(d) != 1:
    raise Exception("life is too short to do timebase math for a silly optimization")
timebase = int(q)

d, q = stream["avg_frame_rate"].split('/')
if int(q) != 1:
    raise Exception("I will figure out NTSC video later")
framerate = int(d)


# pprint.pp(stream)
print(gopentry)
print(PtsToFramenum(timebase, framerate, gopentry["pts"]))
print(gopexit)
print(PtsToFramenum(timebase, framerate, gopexit["pts"]))