import {VideoRTC} from './video-rtc.js';

class VideoStream extends VideoRTC {
  oninit() {
    this.background = true;
    this.visibilityCheck = false;
    super.oninit();
    this.video.controls = false;
    this.video.muted = true;
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.style.objectFit = 'contain';
    this.video.style.background = '#000';
  }

  // Browser autoplay policy mutes autoplaying video; a user gesture is required
  // to unmute. The live-view "Sound" button calls this. Audio (Opus) is already
  // present in the WebRTC stream from go2rtc; we just unmute the element.
  unmute() {
    this.video.muted = false;
    this.video.volume = 1.0;
    const p = this.video.play();
    if (p && p.catch) p.catch(() => {});
    return !this.video.muted;
  }

  mute() { this.video.muted = true; return this.video.muted; }
}

if (!customElements.get('video-stream')) {
  customElements.define('video-stream', VideoStream);
}
