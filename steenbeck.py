#!/usr/bin/env python

"""
Start of the steenbeck project

"""
import subprocess
import json
import argparse
import time
import datetime
from python_get_resolve import GetResolve

#TODO(dmo): figure out what this temporary directory actually needs to be
TEMPDIR = 'C:\\Users\\danie\\Videos\\splicetests\\temporaries'

# compute the longest common subsequence. This will make it possible
# for us to get the "diff" between the 2 timelines
def longestcommonsub(S1, S2):
    m = len(S1)
    n = len(S2)

    # TODO(dmo): trim common ends and beginnings
    L = [[0 for x in range(n+1)] for x in range(m+1)]

    for i in range(m+1):
        for j in range(n+1):
            if i == 0 or j == 0:
                L[i][j] = 0
            elif S1[i-1] == S2[j-1]:
                L[i][j] = L[i-1][j-1] + 1
            else:
                L[i][j] = max(L[i-1][j], L[i][j-1])

    index = L[m][n]

    lcs_algo = [None] * (index)

    i = m
    j = n
    while i > 0 and j > 0:

        if S1[i-1] == S2[j-1]:
            lcs_algo[index-1] = S1[i-1]
            i -= 1
            j -= 1
            index -= 1

        elif L[i-1][j] > L[i][j-1]:
            i -= 1
        else:
            j -= 1
            
    return lcs_algo

def FindTimeline(project):
    cnt = project.GetTimelineCount()
    for i in range(1, cnt+1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == args.t:
            return tl
        
    raise Exception(f"Could not find timeline {args.t}")


def GetTimelines( project ):
    originalTl = FindTimeline(project)
    targetTl = project.GetCurrentTimeline()
    return originalTl, targetTl

def GetProject( resolve ):
    projectManager = resolve.GetProjectManager()
    return projectManager.GetCurrentProject()

def FindKeyframe(frames):
    for i in frames:
        if i["flags"] == "K__":
            return i
    
    raise Exception("could not find keyframe")

parser = argparse.ArgumentParser()
parser.add_argument('-t')
parser.add_argument('-f')
parser.add_argument('-o')
parser.add_argument('-debuglogs', action='store_true')
parser.add_argument('-debuguniquename', action='store_true')
parser.add_argument('-debugreport', action='store_true')

args = parser.parse_args()

resolve = GetResolve()
project = GetProject(resolve)
originalTimeline, targetTimeline = GetTimelines(project)

if originalTimeline.GetStartFrame() != targetTimeline.GetStartFrame():
    raise Exception("differing start frames")

# run through every frame of the video, creating a hash of the current frame
# properties. This is horribly inefficient, but the alternative
# is to actually do the maths for insertion and doing "data structures"
# and "computer science". Life's too short
# TODO(dmo): this is special cased to only handle one track right now
originalitems = originalTimeline.GetItemListInTrack('video', 1)
targetitems = targetTimeline.GetItemListInTrack('video', 1)

def calculateFrameSeq(items):
    frames = []
    for i in items:
        # TODO(dmo): when we make this work for multiple tracks, this
        # obviously has to change. It is not guaranteed to be contiguous over
        # every frame
        # also, we need to look at file properties. this will especially become
        # important when we have to work with titles, which will change text
        name = i.GetName()
        start = i.GetStart()
        end = i.GetEnd()
        # this is not robust in the face of time stretching.
        # Thankfully this is only relevant if someone were to change the speed
        # of a clip.
        # TODO(dmo): figure out what this looks like for source clips
        # with a different framerate than the timeline
        sourcestartframe = i.GetSourceStartFrame()
        # davinci will return 0 for both the first frame of the source
        # and the frame after that. This makes the frame math infuriatingly
        # special cased. The way to determine if we're inserting from the first
        # frame is to see if we have any offset available on the left. This
        # might be bounded by a transition overlay, but for this case, we can
        # assume that no one is doing transitions on the absolutely first frame
        # on of a clip
        if sourcestartframe == 0 and i.GetLeftOffset(False) != 0:
            sourcestartframe += 1

        i = sourcestartframe
        for r in range(start,end):
            #TODO(dmo): make this a hash of relevant values
            frames.append((name,i))
            i += 1

    return frames

originalFrames = calculateFrameSeq(originalitems)
targetFrames = calculateFrameSeq(targetitems)
lcs = longestcommonsub(originalFrames, targetFrames)

# turn out LCS into a list of segments. These segments
# are either "original" or "target" segments.
# for original segments, we use the frame number, 0-indexed of the
# video file on disk
#
# for target segments, we use the frame number of the timeline we read
# from resolve
class segment:
    def __init__(self, originalframe, targetframe, duration):
        self.originalframe = originalframe
        self.targetframe = targetframe
        self.duration = duration
    def __repr__(self) -> str:
        return f"<{type(self).__name__} of:{self.originalframe} tf:{self.targetframe} dur:{self.duration}>"

class original(segment):
    pass
class target(segment):
    pass

segments = []
origptr = 0
# sentinel value for when a frame number isn't valid
# used for marking target frames in segments that should be taken from
# the original file or original frames in target segments
NOFRAME = None
s, i, j = 0, 0, 0
while s < len(lcs) and i < len(originalFrames) and j < len(targetFrames):
    if lcs[s] != targetFrames[j]:
        # insertion, emit an "original" segment, unless we are at the start
        # of the video
        if i != 0:
            duration = i-origptr
            segments.append(original(origptr, j-duration, duration))
        
        oldj = j
        while lcs[s] != targetFrames[j]:
            j += 1
        segments.append(target(NOFRAME, oldj, j-oldj))
        origptr = i
    elif lcs[s] != originalFrames[i]:
        # deletion, emit an "original" section for up until the delete
        # sequence
        if i != 0:
            duration = i-origptr
            segments.append(original(origptr, j-duration, duration))
        while lcs[s] != originalFrames[i]:
            i += 1
        origptr = i       
    else:
        s += 1
        i += 1
        j += 1

# after the loop, if we match on the ending segment,
# emit it
if lcs[s-1] == originalFrames[i-1] == targetFrames[j-1]:
    duration = i-origptr
    segments.append(original(origptr, j-duration, i-origptr))

# figure out which keyframes we need to find, either before or after
# a given point
prevIDR = {}
nextIDR = {}
# the first segment will never need to have it's entry keyframe checked
# skip it
for i in range(1, len(segments)):
    s = segments[i]
    if isinstance(s, target):
        prev = segments[i-1]
        pi = prev.originalframe + prev.duration
        prevIDR[pi] = True
        if isinstance(prev, target):
            raise Exception("should not ever get 2 target segments next to eachother")

        # no segment after this, no need to figure out the out keyframe
        if i == len(segments)-1:
            continue
        nexts = segments[i+1]
        if isinstance(nexts, target):
            raise Exception("should not ever get 2 target segments next to eachother")
        nextIDR[nexts.originalframe] = True
    else:
        prev = segments[i-1]
        if isinstance(prev, original):
            prevIDR[prev.originalframe + prev.duration] = True
        nextIDR[s.originalframe] = True

# construct an interval string for ffmpeg.
# Seeking will give us the first keyframe previous to the seek point
# 100 frames after is a guess for when we will see a keyframe again.
framerate = targetTimeline.GetSetting("timelineFrameRate")
intervals = []
for i in prevIDR:
    second = i/framerate
    intervals.append(f"{second}%+#100")
for i in nextIDR:
    second = i/framerate
    intervals.append(f"{second}%+#100")
intervalstr = ",".join(intervals)

# invoke ffmpeg and have it give us the frames that
command = [
    "./ffprobe.exe",
    "-print_format", "json",
    "-select_streams", "v:0",
    "-show_streams",
    "-show_packets",
    "-read_intervals", intervalstr,
    "-i", args.f
]
res = subprocess.run(command, capture_output=True)
ffprobeoutput = json.loads(res.stdout)
packets = ffprobeoutput["packets"]
# TODO(dmo): figure out more complex streams
stream = ffprobeoutput["streams"][0]

# dedupe all the packets, sort by presentation timestamp
bypts = {}
for i in packets:
    bypts[i["pts"]] = i
packets = sorted(list(bypts.values()), key=lambda x: x["pts"])

# find the timebase, we use this to go from davinci frame numbers to
# ffmpeg packet timestamps
d, q = stream["time_base"].split('/')
if int(d) != 1:
    raise Exception("life is too short to do timebase math for a silly optimization")
timebase = int(q)

# grab the framerate, we will use this later for NTSC
d, q = stream["avg_frame_rate"].split('/')
if int(q) != 1:
    raise Exception("I will figure out NTSC video later")
framerate = int(d)

# find all the keyframes that we need before a given point
# TODO(dmo): clean up, this is ugly
ptsperframe = timebase/framerate

def findprevIDR(packets, i):
    for j in reversed(range(i)):
        if packets[j]["flags"] == "K__":
            return packets[j]["pts"]

def findnextIDR(packets, i):
    for j in range(i, len(packets)):
        if packets[j]["flags"] == "K__":
            return packets[j]["pts"]
    
    # reached the end of the packet stream. We can get here
    # either by not finding a keyframe or reaching the end
    # of the video file
    pkt = packets[j]
    if pkt["pts"] + pkt["duration"] == stream["duration_ts"]:
        return pkt["pts"]
    
    raise Exception("did not find following keyframe")

for pi in prevIDR:
    ptstofind = pi * ptsperframe
    # TODO(dmo): could do binary search or something
    for i, p in enumerate(packets):
        if p["pts"] == ptstofind:
            prevIDR[pi] = findprevIDR(packets, i)/ptsperframe

for ni in nextIDR:
    ptstofind = ni * ptsperframe
    for i, p in enumerate(packets):
        if p["pts"] == ptstofind:
            nextIDR[ni] = findnextIDR(packets, i)/ptsperframe

if args.debuglogs:
    print("segment list before keyframe nudges")
    for s in segments:
        print(s)
    print()

newsegments = []
# create a new sequence with the in and out points 
# of our segments nudged based on the data we found from ffmpeg
for i, s in enumerate(segments[1:], 1):
    lastsegment = segments[i-1]
    if isinstance(s, target):
        of = lastsegment.originalframe + lastsegment.duration
        innudge = of - prevIDR[of]
        s.targetframe -= innudge
        s.duration += innudge
        lastsegment.duration -= innudge
        newsegments.append(lastsegment)
        newsegments.append(s)

        nextseg = segments[i+1]
        outnudge = nextIDR[nextseg.originalframe] - nextseg.originalframe
        s.duration += outnudge
        nextseg.originalframe += outnudge
        nextseg.duration -= outnudge
        newsegments.append(nextseg)
    else:
        # we have already had our in point nudged
        if isinstance(lastsegment, target):
            continue

        # after this point, the previous segment is an original one.
        # that means that we have a discontinuity and we need to
        # ask resolve for a glue segment that covers the cut
        of = lastsegment.originalframe + lastsegment.duration
        innudge = of - prevIDR[of]
        lastsegment.duration -= innudge
        
        outnudge = nextIDR[s.originalframe] - s.originalframe
        newsegments.append(lastsegment)
        tgt = target(None, s.targetframe - innudge, innudge+outnudge)
        newsegments.append(tgt)
        s.originalframe += outnudge
        s.duration -= outnudge
        newsegments.append(s)

if args.debuglogs:
    print("segment list after keyframe nudges")
    for s in newsegments:
        print(s)
    print()
segments = newsegments

# consistency check
length = 0
for s in segments:
    of = s.originalframe
    tf = s.targetframe
    if (of != NOFRAME and of < 0) or (tf != NOFRAME and tf < 0) or s.duration < 0:
        raise Exception("overlapping segments, contact developer")
    length += s.duration

if length != targetTimeline.GetEndFrame() - targetTimeline.GetStartFrame():
    raise Exception("made a sequence that is not same length as intended result, contact developer")

# look for the latest render job that matches the video file
def findRender(renders):
    for r in reversed(renders):
        if r["TargetDir"] + '\\' + r["OutputFilename"] == args.f:
            return r
        
    raise Exception("couldn't find template render")

# use the last render as a template for our glue files
templateRender = findRender(project.GetRenderJobList())
jobs = []
for i, s in enumerate(segments):
    if isinstance(s, target):
        rendersettings = {
            "IsExportAudio": False,
            "FormatWidth": templateRender["FormatWidth"],
            "FormatHeight": templateRender["FormatHeight"],
            "MarkIn": originalTimeline.GetStartFrame() + s.targetframe,
            "MarkOut": originalTimeline.GetStartFrame() + (s.targetframe+s.duration)-1,
            #TODO(dmo): figure out where to store this temporary file
            'TargetDir': TEMPDIR,
            'CustomName': f'glue{i}.mov'
        }
        project.SetRenderSettings(rendersettings)
        job = project.AddRenderJob()
        jobs.append(job)

project.StartRendering(jobs, isInteractiveMode=False)

while project.IsRenderingInProgress():
    time.sleep(1)

for j in jobs:
    status = project.GetRenderJobStatus(j)
    if status['JobStatus'] != 'Complete':
        raise Exception(f"{j} render failed")

# generate file for ffmpeg concat demuxer
splicelines = []
for i, s in enumerate(segments):
    if isinstance(s, original):
        if s.duration == 0:
            continue
        splicelines.append(f"file '{args.f}'")
        splicelines.append(f"inpoint {s.originalframe/framerate}")
        # ffmpeg goes by decode timestamp when determining when to stop concatenating
        # and the outpoint is exclusive, so we need to specify the frame before the keyframe
        # Also, specify a duration since without this, it will take use the outpoint
        # and mess up the presentation timestamp for the following file
        splicelines.append(f"outpoint {(s.originalframe+s.duration-1)/framerate}")
        splicelines.append(f"duration {s.duration/framerate}")
    else:
        if s.duration <= 0:
            raise Exception("zero length 'to' segment, contact developer")
        splicelines.append(f"file '{TEMPDIR}\\glue{i}.mov'")

fileloc = f"{TEMPDIR}\\splice.txt"
with open(fileloc, "w") as splicefile:
    splicefile.write("\n".join(splicelines))

outputfile = args.o
if args.debuguniquename:
    now = datetime.datetime.now()
    outputfile = f"output-{now.strftime("%y%m%d-%H%M%S")}.mov"

reportflag = None
if args.debugreport:
    reportflag = "-report"

command = [
    "./ffmpeg.exe",
    "-y",
    reportflag,
    "-safe", "0",
    "-f", "concat",
    "-i", fileloc,
    "-c", "copy",
    # TODO(dmo): for some weird ffmpeg produces a stupid file if we select audio
    "-map", "0:v:0",
    outputfile
]
command = [i for i in command if i is not None]
res = subprocess.run(command)
