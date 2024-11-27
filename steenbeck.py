#!/usr/bin/env python

"""
Start of the steenbeck project

"""
import subprocess
import json
import pprint
import argparse
from python_get_resolve import GetResolve

def longestcommonsub(S1, S2):
    m = len(S1)
    n = len(S2)

    # TODO: trim common ends and beginnings
    L = [[0 for x in range(n+1)] for x in range(m+1)]

    # Building the mtrix in bottom-up way
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
        
    raise Exception("Could not find timeline {}".format(args.t))


def GetTimelines( project ):
    fromTl = FindTimeline(project)
    toTl = project.GetCurrentTimeline()
    return fromTl, toTl

def GetProject( resolve ):
    projectManager = resolve.GetProjectManager()
    return projectManager.GetCurrentProject()

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
project = GetProject(resolve)
fromTimeline, toTimeline = GetTimelines(project)

if fromTimeline.GetStartFrame() != toTimeline.GetStartFrame():
    raise Exception("differing start frames")

# run through every frame of the video, creating a hash of the current frame
# properties. This is horribly inefficient, but the alternative
# is to actually do the maths for insertion and doing "data structures"
# and "computer science". Life's too short
# TODO(dmo): this is special cased to only handle one track right now
fromitems = fromTimeline.GetItemListInTrack('video', 1)
toitems = toTimeline.GetItemListInTrack('video', 1)

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

fromFrames = calculateFrameSeq(fromitems)
toFrames = calculateFrameSeq(toitems)
lcs = longestcommonsub(fromFrames, toFrames)

# turn out LCS into a list of segments. These segments
# are either "from" or "to" segments.
# for from segments, we use the frame number 0 indexed of the
# video file on disk
#
# for to segments, we use the frame number of the timeline we read
# from resolve
class segment:
    def __init__(self, src, startframe, duration):
        self.src = src
        self.startframe = startframe
        self.duration = duration


segments = []
fromptr = 0
s, i, j = 0, 0, 0
while s < len(lcs) and i < len(fromFrames) and j < len(toFrames):
    if lcs[s] != toFrames[j]:
        # insertion, emit a "from" segment, unless we are at the start
        # of the video
        if i != 0:
            segments.append(segment("From", fromptr, i-fromptr))
        
        oldj = j
        while lcs[s] != toFrames[j]:
            j += 1
        segments.append(segment("To", oldj, j-oldj))
        fromptr = i
    elif lcs[s] != fromFrames[i]:
        # deletion, emit a "from" section for up until the delete
        # sequence
        raise Exception("deletion not implemented yet")
        if i != 0:
            segments.append(segment("From", fromptr, i-fromptr))
        while lcs[s] != fromFrames[i]:
            i += 1
        fromptr = i
        
    else:
        s += 1
        i += 1
        j += 1

# after the loop, if we match on the ending segment,
# emit it
if lcs[s-1] == fromFrames[i-1] == toFrames[j-1]:
    segments.append(segment("From", fromptr, i-fromptr))

# figure out which keyframes we need to find, either before or after
# a given point
prevIDR = {}
nextIDR = {}
for i in range(len(segments)):
    s = segments[i]
    if s.src == "To":
        if i != 0:
            prev = segments[i-1]
            pi = prev.startframe + prev.duration
            prevIDR[pi] = True
            if prev.src == "To":
                raise Exception("should not ever get 2 To segments next to eachother")

        nexts = segments[i+1]
        if nexts.src == "To":
            raise Exception("should not ever get 2 To segments next to eachother")
        nextIDR[nexts.startframe] = True

# construct an interval string for ffmpeg.
# Seeking will give us the first keyframe previous to the seek point
# 100 frames after is a guess for when we will see a keyframe again
framerate = toTimeline.GetSetting("timelineFrameRate")
intervals = []
for i in prevIDR:
    second = i/framerate
    intervals.append("{}%+#100".format(second))
for i in nextIDR:
    second = i/framerate
    intervals.append("{}%+#100".format(second))
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
stream = ffprobeoutput["streams"][0]

# dedupe, sort by pts
bypts = {}
for i in packets:
    bypts[i["pts"]] = i
packets = sorted(list(bypts.values()), key=lambda x: x["pts"])

# find the timebase, we use this to go from frame numbers to identifying packets
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
for pi in prevIDR:
    ptstofind = pi * ptsperframe
    # TODO(dmo): could do binary search or something
    for i, p in enumerate(packets):
        if p["pts"] == ptstofind:
            for j in reversed(range(i)):
                if packets[j]["flags"] == "K__":
                    prevIDR[pi] = packets[j]["pts"]/ptsperframe
                    break
            break

for ni in nextIDR:
    ptstofind = pi * ptsperframe
    for i, p in enumerate(packets):
        if p["pts"] == ptstofind:
            for j in range(i, len(packets)):
                if packets[j]["flags"] == "K__":
                    nextIDR[ni] = packets[j]["pts"]/ptsperframe
                    break
            break

print("before")
for s in segments:
    print(s.src, s.startframe, s.duration)

print()

# nudge the in and out points of all of our segments
# based on the data we found from ffmpeg
for i, s in enumerate(segments):
    if s.src == "To":
        sf = s.startframe
        innudge = sf - prevIDR[sf]
        s.startframe -= innudge
        s.duration += innudge
        segments[i-1].duration -= innudge

        nextseg = segments[i+1]
        outnudge = nextIDR[nextseg.startframe] - nextseg.startframe
        s.duration += outnudge
        nextseg.startframe += outnudge
        nextseg.duration -= outnudge

print("after")
for s in segments:
    print(s.src, s.startframe, s.duration)

# consistency check
length = 0
for s in segments:
    if s.startframe < 0 or s.duration < 0:
        raise Exception("overlapping segments, contact developer")
    length += s.duration

if length != toTimeline.GetEndFrame() - toTimeline.GetStartFrame():
    raise Exception("made a sequence that is not same length as intended result, contact developer")

# look for the latest render job that matches the video file
# that matches the to file
def findRender(renders):
    for r in reversed(renders):
        if r["TargetDir"] + '\\' + r["OutputFilename"] == args.f:
            return r
        
    raise Exception("couldn't find template render")

templateRender = findRender(project.GetRenderJobList())
rendersettings = {
    "IsExportAudio": False,
    "FormatWidth": templateRender["FormatWidth"],
    "FormatHeight": templateRender["FormatHeight"],
#    "MarkIn": fromTimeline.GetStartFrame() + renderentry,
#    "MarkOut": fromTimeline.GetStartFrame() + (renderexit-1),
    #TODO(dmo): figure out where to store this temporary file
    'TargetDir': 'C:\\Users\\danie\\Videos\\splicetests',
    'CustomName': 'glue.mov'
}
project.SetRenderSettings(rendersettings)
job = project.AddRenderJob()
project.StartRendering(job)

