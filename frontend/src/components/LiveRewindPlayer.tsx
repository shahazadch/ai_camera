import Hls from "hls.js";
import { useEffect, useRef, useState } from "react";

const JUMPS: Array<[string, number]> = [
  ["⏪ 10 min", 10 * 60],
  ["⏪ 30 min", 30 * 60],
  ["⏪ 1 hour", 60 * 60],
];

/**
 * Plays a camera's live-rewind buffer (HLS written by the recorder): live, or
 * jump back up to the rewind window. Shows the real clock time being watched.
 */
export default function LiveRewindPlayer({ src, rewindMinutes }: { src: string; rewindMinutes: number }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [watching, setWatching] = useState<Date | null>(null);
  const [available, setAvailable] = useState(0);
  const [behind, setBehind] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    setError("");
    if (Hls.isSupported()) {
      const hls = new Hls({ liveDurationInfinity: true, backBufferLength: 120, lowLatencyMode: false });
      hlsRef.current = hls;
      hls.loadSource(src);
      hls.attachMedia(video);
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else setError("This browser can't play this camera's video (try Safari or Chrome).");
      });
      return () => {
        hls.destroy();
        hlsRef.current = null;
      };
    }
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = src; // Safari plays HLS natively
      return () => {
        video.removeAttribute("src");
        video.load();
      };
    }
    setError("This browser can't play live rewind. Use Chrome, Edge or Safari.");
    return undefined;
  }, [src]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const update = () => {
      const seekable = video.seekable;
      if (seekable.length) {
        const start = seekable.start(0);
        const end = seekable.end(seekable.length - 1);
        setAvailable(Math.max(0, end - start));
        setBehind(Math.max(0, end - video.currentTime));
      }
      const date = hlsRef.current?.playingDate;
      setWatching(date ? new Date(date) : null);
    };
    video.addEventListener("timeupdate", update);
    video.addEventListener("progress", update);
    return () => {
      video.removeEventListener("timeupdate", update);
      video.removeEventListener("progress", update);
    };
  }, []);

  function jumpBack(seconds: number) {
    const video = videoRef.current;
    if (!video || !video.seekable.length) return;
    const start = video.seekable.start(0);
    const end = video.seekable.end(video.seekable.length - 1);
    video.currentTime = Math.max(start, end - seconds);
    void video.play();
  }

  function goLive() {
    const video = videoRef.current;
    if (!video) return;
    const liveAt = hlsRef.current?.liveSyncPosition;
    if (liveAt) video.currentTime = liveAt;
    else if (video.seekable.length) video.currentTime = video.seekable.end(video.seekable.length - 1);
    void video.play();
  }

  const isLive = behind < 20;

  return (
    <div className="recording-player live-rewind">
      <div className="recording-player-head">
        <strong>
          {isLive ? <span className="live-dot">● LIVE</span> : `⏪ ${Math.round(behind / 60)} min behind live`}
          {watching ? ` · ${watching.toLocaleTimeString()}` : ""}
        </strong>
        <span className="live-rewind-buttons">
          {JUMPS.map(([label, seconds]) => (
            <button
              key={label}
              type="button"
              disabled={seconds > available + 5}
              title={seconds > available + 5 ? "Not recorded that far back yet" : ""}
              onClick={() => jumpBack(seconds)}
            >
              {label}
            </button>
          ))}
          <button type="button" disabled={isLive} onClick={goLive}>
            Live ▶
          </button>
        </span>
      </div>
      {error ? (
        <div className="error-banner">{error}</div>
      ) : (
        <video ref={videoRef} controls autoPlay muted playsInline />
      )}
      <small className="live-rewind-note">
        Rewind up to {rewindMinutes} min · {Math.floor(available / 60)} min available so far. Drag the bar to go anywhere
        in it.
      </small>
    </div>
  );
}
