#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scene_dir="$script_dir/scenes"
render_dir="$script_dir/rendered"
output_mp4="$script_dir/clusy-crawler-beta4-x.mp4"
output_poster="$script_dir/clusy-crawler-beta4-poster.png"

command -v rsvg-convert >/dev/null
command -v ffmpeg >/dev/null
mkdir -p "$render_dir"

for scene in 01-noise 02-signal 03-transform 04-proof 05-cta; do
  rsvg-convert \
    --width 1920 \
    --height 1080 \
    --output "$render_dir/$scene.png" \
    "$scene_dir/$scene.svg"
done

cp "$render_dir/05-cta.png" "$output_poster"

ffmpeg -hide_banner -loglevel warning -y \
  -loop 1 -framerate 30 -t 2.5 -i "$render_dir/01-noise.png" \
  -loop 1 -framerate 30 -t 2.2 -i "$render_dir/02-signal.png" \
  -loop 1 -framerate 30 -t 2.5 -i "$render_dir/03-transform.png" \
  -loop 1 -framerate 30 -t 3.4 -i "$render_dir/04-proof.png" \
  -loop 1 -framerate 30 -t 3.2 -i "$render_dir/05-cta.png" \
  -filter_complex "
    [0:v]zoompan=z='min(1.000+0.00018*on,1.018)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv444p,settb=AVTB,setpts=PTS-STARTPTS[s0];
    [1:v]zoompan=z='max(1.018-0.00016*on,1.002)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv444p,settb=AVTB,setpts=PTS-STARTPTS[s1];
    [2:v]zoompan=z='min(1.000+0.00016*on,1.016)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv444p,settb=AVTB,setpts=PTS-STARTPTS[s2];
    [3:v]zoompan=z='max(1.016-0.00014*on,1.002)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv444p,settb=AVTB,setpts=PTS-STARTPTS[s3];
    [4:v]zoompan=z='min(1.000+0.00012*on,1.012)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=30,format=yuv444p,settb=AVTB,setpts=PTS-STARTPTS[s4];
    [s0][s1]xfade=transition=wipeleft:duration=0.5:offset=2.0[x1];
    [x1][s2]xfade=transition=smoothleft:duration=0.5:offset=3.7[x2];
    [x2][s3]xfade=transition=circleopen:duration=0.5:offset=5.7[x3];
    [x3][s4]xfade=transition=fade:duration=0.5:offset=8.6[x4];
    color=c=0x17D1A6:s=1920x8:r=30:d=11.8[progress];
    [x4][progress]overlay=x='-1920+162.711864*t':y=1072:eval=frame:shortest=1,format=yuv420p[v]
  " \
  -map "[v]" \
  -an \
  -c:v libx264 \
  -preset medium \
  -crf 18 \
  -profile:v high \
  -level 4.1 \
  -movflags +faststart \
  -metadata title="Clusy Crawler Beta 4" \
  -metadata comment="Registered Beta 2 AEB article_body evidence; Beta 4 launch animation" \
  "$output_mp4"

printf 'Rendered %s\n' "$output_mp4"
printf 'Rendered %s\n' "$output_poster"
