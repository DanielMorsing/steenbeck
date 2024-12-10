#!/usr/bin/env python

"""
Steenbeck does partial rendering on davinci resolve, speeding up
re-renders. It works by taking an original timeline, a render of that timeline
and a target timeline. It will then render every new addition and stitch it together
with ffmpeg in the end. This turns a full h264 encode into just the added section,
speeding up renders.
"""
import subprocess
import json
import argparse
import time
import datetime
import hashlib
import marshal
from python_get_resolve import GetResolve


def steenbeck():
    # TODO(dmo): figure out what this temporary directory actually needs to be
    TEMPDIR = 'C:\\Users\\danie\\Videos\\splicetests\\temporaries'

    parser = argparse.ArgumentParser()
    parser.add_argument('-t')
    parser.add_argument('-f')
    parser.add_argument('-o')
    parser.add_argument('-debuglogs', action='store_true')
    parser.add_argument('-debuguniquename', action='store_true')
    parser.add_argument('-debugreport', action='store_true')

    args = parser.parse_args()

    def dumpsegments(msg, segs):
        if not args.debuglogs:
            return

        print(msg)
        for s in segs:
            print(s)
        print()

    resolve = GetResolve()
    project = GetProject(resolve)
    originalTimeline, targetTimeline = GetTimelines(project, args.t)

    if originalTimeline.GetStartFrame() != targetTimeline.GetStartFrame():
        raise Exception("differing start frames")

    # run through every frame of the video, creating a hash of the current frame
    # properties. This is horribly inefficient, but the alternative
    # is to actually do the maths for insertion and doing "data structures"
    # and "computer science". Life's too short
    originalFrames = calculateFrameSeq(originalTimeline)
    targetFrames = calculateFrameSeq(targetTimeline)

    lcs = longestcommonsub(originalFrames, targetFrames)

    # turn our LCS into a list of segments. These segments
    # are either "original" or "target" segments.
    # for original segments, we use the frame number, 0-indexed of the
    # video file on disk
    #
    # for target segments, we use the frame number of the timeline we read
    # from resolve
    class segment:
        def __init__(self, originalframe, positiondelta, duration):
            self.originalframe = originalframe
            self.positiondelta = positiondelta
            self.duration = duration

            self.inKeyframe = None
            self.outKeyframe = None

        def __repr__(self) -> str:
            return f"<{type(self).__name__} of:{self.originalframe} delta:{self.positiondelta} dur:{self.duration} inframe:{self.inKeyframe} outframe:{self.outKeyframe}>"

    class original(segment):
        pass

    class target(segment):
        pass

    segments = []
    s, i, j = 0, 0, 0

    def sequenceleft():
        return s < len(lcs) and i < len(originalFrames) and j < len(targetFrames)
    while sequenceleft():
        # walk sequences until we don't match
        oldi = i
        while sequenceleft() and (lcs[s] == originalFrames[i] == targetFrames[j]):
            s += 1
            i += 1
            j += 1
        if oldi != i:
            segments.append(original(oldi, j-i, i-oldi))

        if not sequenceleft():
            break

        # this is an insertion, walk target frames until we
        # match and emit a target segment
        if lcs[s] != targetFrames[j]:
            oldj = j
            while lcs[s] != targetFrames[j]:
                j += 1
            segments.append(target(oldj, 0, j-oldj))

        # deletion, walk until we match up again
        if lcs[s] != originalFrames[i]:
            while lcs[s] != originalFrames[i]:
                i += 1

    dumpsegments("segment list before keyframe search", segments)

    # for every segment, find the keyframe after its in point and the one before it's out point
    # these will act as "handles" when we start gluing segments together
    inKeyframe = {}
    outKeyframe = {}
    for s in segments:
        # target segments do not have keyframes at their entry or exit
        # rely on them being next to original segments that do
        if isinstance(s, target):
            continue

        inKeyframe[s.originalframe] = s
        outframe = s.originalframe+s.duration
        if outframe < len(originalFrames):
            outKeyframe[outframe] = s
        else:
            s.outKeyframe = outframe

    # construct an interval string for ffmpeg.
    # Seeking will give us the first keyframe previous to the seek point.
    # 100 frames after is a guess for when we will see a keyframe again.
    framerate = targetTimeline.GetSetting("timelineFrameRate")
    intervals = []
    for i in inKeyframe:
        second = i/framerate
        intervals.append(f"{second}%+#100")
    for i in outKeyframe:
        second = i/framerate
        intervals.append(f"{second}%+#100")
    intervalstr = ",".join(intervals)

    # invoke ffmpeg to find out where the keyframes are
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
        raise Exception(
            "life is too short to do timebase math for a silly optimization")
    timebase = int(q)

    # grab the framerate, luckily we know these files are one
    # framerate, since davinci only supports fixed framerates
    d, q = stream["avg_frame_rate"].split('/')
    # TODO(dmo): figure out NTSC video
    if int(q) != 1:
        raise Exception("I will figure out NTSC video later")
    framerate = int(d)

    # find all the keyframes that we need before a given point
    # TODO(dmo): clean up, this is ugly
    ptsperframe = timebase/framerate

    def islastframe(pkt):
        return pkt["pts"] + pkt["duration"] == stream["duration_ts"]

    def findprevKeyframe(packets, i):
        for j in reversed(range(i)):
            if packets[j]["flags"] == "K__":
                return packets[j]["pts"]

    def findnextKeyframe(packets, i):
        for j in range(i, len(packets)):
            if packets[j]["flags"] == "K__":
                return packets[j]["pts"]

        # reached the end of the packet stream. We can get here
        # either by not finding a keyframe or reaching the end
        # of the video file
        if islastframe(packets[j]):
            return packets[j]["pts"]
        raise Exception("did not find following keyframe")

    for i, p in enumerate(packets):
        framenum = p["pts"]/ptsperframe
        if framenum in inKeyframe:
            keyframe = findnextKeyframe(packets, i)/ptsperframe
            inKeyframe[framenum].inKeyframe = keyframe
        if framenum in outKeyframe:
            keyframe = findprevKeyframe(packets, i)/ptsperframe
            outKeyframe[framenum].outKeyframe = keyframe

    dumpsegments("after keyframe search", segments)

    # go through every segment, if any of the original segments have overlapping
    # in and out keyframes, turn the segment into a target one
    for i, s in enumerate(segments):
        if isinstance(s, target):
            continue
        if s.inKeyframe >= s.outKeyframe:
            segments[i] = target(
                s.originalframe + s.positiondelta, 0, s.duration)

    dumpsegments("after overlap target morph", segments)

    # the previous pass can cause target segments next to each other.
    # "roll up" consecutive target segments into one segment
    newsegments = []
    targetaccum = None
    for i, s in enumerate(segments):
        if isinstance(s, original):
            if targetaccum != None:
                newsegments.append(targetaccum)
                targetaccum = None
            newsegments.append(s)
            continue
        if targetaccum == None:
            targetaccum = s
        elif targetaccum.originalframe + targetaccum.duration == s.originalframe:
            targetaccum.duration += s.duration
        else:
            newsegments.append(targetaccum)
            targetaccum = None
    if targetaccum != None:
        newsegments.append(targetaccum)

    segments = newsegments
    dumpsegments("segment list before keyframe nudges", segments)

    # create a new sequence with the in and out points
    # of our segments nudged based on the data we found from ffmpeg
    for i, s in enumerate(segments):
        if isinstance(s, target):
            continue

        innudge = s.inKeyframe - s.originalframe
        outnudge = (s.originalframe + s.duration) - s.outKeyframe
        if innudge > 0:
            prevsegment = segments[i-1]
            prevsegment.duration += innudge
            s.duration -= innudge
            s.originalframe += innudge
        if outnudge > 0 and i != len(segments)-1:
            nextseg = segments[i+1]
            nextseg.duration += outnudge
            nextseg.originalframe -= outnudge
            s.duration -= outnudge

    dumpsegments("segment list after keyframe nudges", segments)
    # after we've nudged the cut points, find any segment
    # that still has a difference between its outframe
    # and the outgoing keyframe. This indicates a spot
    # that needs glue
    newsegments = []
    for i, s in enumerate(segments):
        if isinstance(s, target):
            newsegments.append(s)
            continue
        outframe = s.originalframe + s.duration
        if s.outKeyframe < outframe:
            tgtduration = outframe - s.outKeyframe
            s.duration -= tgtduration
            tgtframe = s.originalframe + s.positiondelta
            tgtframe += s.duration
            newsegments.append(s)
            newsegments.append(target(tgtframe, 0, tgtduration))
        else:
            newsegments.append(s)

    segments = newsegments
    dumpsegments("segment list after glue insertion", segments)
    # consistency check
    length = 0
    for s in segments:
        of = s.originalframe
        if of < 0 or s.duration < 0:
            raise Exception("overlapping segments, contact developer")
        length += s.duration

    if length != targetTimeline.GetEndFrame() - targetTimeline.GetStartFrame():
        raise Exception(
            "made a sequence that is not same length as intended result, contact developer")

    # strip audio from the concatenation input
    # the concatenation demuxer can get confused if it
    # encounters audio packets and we're adding the audio
    # back later anyway
    basefile = f"{TEMPDIR}\\base.mov"
    command = [
        "./ffmpeg.exe",
        "-y",
        "-i", args.f,
        "-c", "copy",
        "-map", "0:v",
        basefile
    ]
    res = subprocess.run(command)

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
            targetstart = s.originalframe + s.positiondelta
            rendersettings = {
                "IsExportAudio": False,
                "FormatWidth": templateRender["FormatWidth"],
                "FormatHeight": templateRender["FormatHeight"],
                "MarkIn": originalTimeline.GetStartFrame() + targetstart,
                "MarkOut": originalTimeline.GetStartFrame() + (targetstart+s.duration)-1,
                # TODO(dmo): figure out where to store this temporary file
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
            if s.duration <= 0:
                continue
            splicelines.append(f"file '{basefile}'")
            splicelines.append(f"inpoint {s.originalframe/framerate}")
            # ffmpeg goes by decode timestamp when determining when to stop concatenating
            # and the outpoint is exclusive, so we need to specify the frame before the keyframe
            # Also, specify a duration since without this, it will take use the outpoint
            # and mess up the presentation timestamp for the following file
            splicelines.append(
                f"outpoint {(s.originalframe+s.duration-1)/framerate}")
            splicelines.append(f"duration {s.duration/framerate}")
        else:
            if s.duration <= 0:
                raise Exception("zero length 'to' segment, contact developer")
            splicelines.append(f"file '{TEMPDIR}\\glue{i}.mov'")
            splicelines.append(f"duration {s.duration/framerate}")

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
        # TODO(dmo): select a prerendered audio stream
        "-map", "0:v:0",
        outputfile
    ]
    command = [i for i in command if i is not None]
    res = subprocess.run(command)


def calculateFrameSeq(timeline):
    """
    calculateFrameSeq returns an array of hashes, each representing a frame in the timeline.
    """
    startframe = timeline.GetStartFrame()
    tlduration = timeline.GetEndFrame() - startframe
    frames = [None] * tlduration

    trackcount = timeline.GetTrackCount('video')
    for tc in range(1, trackcount+1):
        items = timeline.GetItemListInTrack('video', tc)
        for it in items:
            name = it.GetName()
            mpi = it.GetMediaPoolItem()
            if mpi is not None:
                mid = mpi.GetMediaId()
                name = mid

            start = it.GetStart() - startframe
            end = it.GetEnd() - startframe
            # this is not robust in the face of time stretching.
            # Thankfully this is only relevant if someone were to change the speed
            # of a clip.
            # TODO(dmo): figure out what this looks like for source clips
            # with a different framerate than the timeline

            sourcestartframe = it.GetSourceStartFrame()
            # source start frame of transitions and compositions is undefined
            if sourcestartframe is None:
                i = 0
            else:
                i = sourcestartframe
                # davinci will return 0 for both the first frame of the source
                # and the frame after that. This makes the frame math infuriatingly
                # special cased. The way to determine if we're inserting from the first
                # frame is to see if we have any offset available on the left. This
                # might be bounded by a transition overlay, but for this case, we can
                # assume that no one is doing transitions on the absolutely first frame
                # on of a clip
                if sourcestartframe == 0 and it.GetLeftOffset(False) != 0:
                    sourcestartframe += 1

            # Get all the properties about this timeline item that we can find
            # This will be added to a running hash that we keep for every
            # frame in the timeline
            props = it.GetProperty()
            # I don't think resolve will give out dicts in random order
            # but let's be safe
            props = dict(sorted(props.items()))
            # I know I'm not supposed to use marshal, but this is just a convenient
            # binary representation of a dictionary
            basehashdata = marshal.dumps({
                "name": name,
                "props": props
            })
            # if the frame has yet to have a hash assigned, use a copy
            # to instantiate it. We lazily instantiate this later if needed
            basehash = None

            for r in range(start, end):
                hash = frames[r]
                if hash is None:
                    if basehash is None:
                        basehash = hashlib.sha256(
                            basehashdata, usedforsecurity=False)
                    hash = basehash.copy()
                else:
                    hash.update(basehashdata)

                hash.update(marshal.dumps(i))
                i += 1
                frames[r] = hash

    return [f.digest() for f in frames]


def longestcommonsub(S1, S2):
    """
    Longest common subsequence
    """
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


def FindTimeline(project, timelinename):
    cnt = project.GetTimelineCount()
    for i in range(1, cnt+1):
        tl = project.GetTimelineByIndex(i)
        if tl.GetName() == timelinename:
            return tl

    raise Exception(f"Could not find timeline {timelinename}")


def GetTimelines(project, origtimeline):
    originalTl = FindTimeline(project, origtimeline)
    targetTl = project.GetCurrentTimeline()
    return originalTl, targetTl


def GetProject(resolve):
    projectManager = resolve.GetProjectManager()
    return projectManager.GetCurrentProject()


if __name__ == "__main__":
    steenbeck()
