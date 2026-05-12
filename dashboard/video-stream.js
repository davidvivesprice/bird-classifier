import {VideoRTC} from './video-rtc.js';

class VideoStream extends VideoRTC {
  oninit() {
    super.oninit();
    this.video.controls = false;
    this.video.muted = true;
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.style.objectFit = 'contain';
    this.video.style.background = '#000';
  }
}

if (!customElements.get('video-stream')) {
  customElements.define('video-stream', VideoStream);
}
