import math
import struct
import wave
import random
import os

SAMPLE_RATE = 48000

def write_wav(filename, samples, sample_rate=SAMPLE_RATE):
    """Write mono or stereo samples (float between -1.0 and 1.0) to 16-bit WAV."""
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    is_stereo = isinstance(samples[0], (tuple, list))
    channels = 2 if is_stereo else 1
    
    with wave.open(filename, 'w') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        
        raw_data = bytearray()
        for s in samples:
            if is_stereo:
                l, r = s
                # Soft clip
                l = max(-0.99, min(0.99, l))
                r = max(-0.99, min(0.99, r))
                il = int(l * 32767.0)
                ir = int(r * 32767.0)
                raw_data.extend(struct.pack('<hh', il, ir))
            else:
                v = max(-0.99, min(0.99, s))
                iv = int(v * 32767.0)
                raw_data.extend(struct.pack('<h', iv))
        wf.writeframes(raw_data)
    print(f"Generated: {filename} ({len(samples)/sample_rate:.2f}s)")

def make_ambient_drone(duration=94.0):
    """Deep, cinematic analog pad drone (A1 chord: 55Hz, 82.4Hz, 110Hz, 164.8Hz)."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Envelope: 4s fade in, 4s fade out
        env = min(1.0, t / 4.0) * min(1.0, (duration - t) / 4.0)
        
        # Subtle slow LFOs for organic movement
        lfo1 = math.sin(2 * math.pi * 0.15 * t)
        lfo2 = math.cos(2 * math.pi * 0.08 * t)
        
        # Fundamental sub-bass + harmonics
        f0 = 55.0  # A1
        sub = 0.35 * math.sin(2 * math.pi * f0 * t)
        fifth = 0.22 * math.sin(2 * math.pi * (f0 * 1.5) * t + lfo1 * 0.2)
        octave = 0.18 * math.sin(2 * math.pi * (f0 * 2.0) * t + lfo2 * 0.3)
        third = 0.12 * math.sin(2 * math.pi * (f0 * 2.5) * t)
        high = 0.06 * math.sin(2 * math.pi * (f0 * 4.0) * t)
        
        # Subtle warm noise bed
        noise = (random.random() * 2 - 1) * 0.015
        
        mono = (sub + fifth + octave + third + high + noise) * env * 0.45
        # Slight stereo separation
        left = mono * (0.9 + 0.1 * lfo1)
        right = mono * (0.9 - 0.1 * lfo1)
        samples.append((left, right))
    return samples

def make_ping(freq=1760.0, duration=0.25):
    """High-frequency crystal electronic snap/ping."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 24.0)
        sig = 0.7 * math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(2 * math.pi * (freq * 2.0) * t)
        samples.append(sig * env * 0.4)
    return samples

def make_glitch(duration=0.45):
    """Electronic glitch / error connection break."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 7.0)
        # Frequency modulation
        mod = math.sin(2 * math.pi * 32.0 * t)
        carrier = math.sin(2 * math.pi * (180.0 + mod * 220.0) * t)
        # Noise bursts
        noise = (random.random() * 2 - 1) * 0.4 if (int(t * 80) % 2 == 0) else 0
        sig = (carrier * 0.6 + noise * 0.4) * env * 0.5
        samples.append((sig, sig))
    return samples

def make_impact_boom(duration=1.8):
    """Cinematic sub-bass boom impact."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 3.5)
        # Pitch drop from 120Hz down to 38Hz
        freq = 38.0 + (120.0 - 38.0) * math.exp(-t * 12.0)
        phase = 2 * math.pi * freq * t
        sub = math.sin(phase)
        # Transient punch at the start
        punch = math.exp(-t * 40.0) * math.sin(2 * math.pi * 80.0 * t) * 0.6
        sig = (sub * 0.8 + punch) * env * 0.75
        samples.append((sig, sig))
    return samples

def make_whoosh(duration=0.65):
    """Airy cinematic stereo whoosh."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Hann window
        env = 0.5 * (1 - math.cos(2 * math.pi * t / duration))
        # Swept noise
        f_center = 200.0 + 2200.0 * math.sin(math.pi * t / duration)
        noise = random.random() * 2 - 1
        sig = noise * math.sin(2 * math.pi * f_center * t) * env * 0.5
        # Pan left to right
        pan = t / duration
        samples.append((sig * (1 - pan), sig * pan))
    return samples

def make_key_click(duration=0.07):
    """Subtle tactile mechanical keyboard click."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        click_env = math.exp(-t * 90.0)
        thock_env = math.exp(-t * 45.0)
        click = math.sin(2 * math.pi * 2400.0 * t) * click_env * 0.3
        thock = math.sin(2 * math.pi * 420.0 * t) * thock_env * 0.4
        noise = (random.random() * 2 - 1) * math.exp(-t * 120.0) * 0.25
        sig = (click + thock + noise) * 0.5
        samples.append((sig, sig))
    return samples

def make_key_return(duration=0.12):
    """Deeper enter/return keystroke."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 30.0)
        thock = math.sin(2 * math.pi * 280.0 * t) * env * 0.55
        snap = math.sin(2 * math.pi * 3200.0 * t) * math.exp(-t * 80.0) * 0.25
        sig = (thock + snap) * 0.55
        samples.append((sig, sig))
    return samples

def make_crystallize(duration=0.85):
    """Modern UI crystal pop / glass resonance."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    f0 = 1180.0
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 5.0)
        h1 = 0.5 * math.sin(2 * math.pi * f0 * t) * math.exp(-t * 4.5)
        h2 = 0.3 * math.sin(2 * math.pi * (f0 * 2.76) * t) * math.exp(-t * 7.0)
        h3 = 0.2 * math.sin(2 * math.pi * (f0 * 5.4) * t) * math.exp(-t * 10.0)
        sig = (h1 + h2 + h3) * env * 0.55
        samples.append((sig * 0.9, sig * 1.1))
    return samples

def make_sonar_pulse(duration=1.2):
    """Ethereal radar / sonar wave pulse."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    f0 = 840.0
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 3.2)
        sine = math.sin(2 * math.pi * f0 * t)
        oct = math.sin(2 * math.pi * (f0 * 2.0) * t) * 0.15
        sig = (sine + oct) * env * 0.45
        samples.append((sig, sig))
    return samples

def make_stage_chime(note_freq=523.25, duration=0.7):
    """Ascending harmonic bell chime for search stages."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 6.5)
        h1 = 0.6 * math.sin(2 * math.pi * note_freq * t)
        h2 = 0.25 * math.sin(2 * math.pi * (note_freq * 2.0) * t)
        h3 = 0.15 * math.sin(2 * math.pi * (note_freq * 3.0) * t)
        sig = (h1 + h2 + h3) * env * 0.5
        samples.append((sig, sig))
    return samples

def make_packet_transmit(duration=0.25):
    """Digital telemetry blip / packet transmission."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 15.0)
        # Fast upward chirp
        freq = 1200.0 + 1600.0 * (t / duration)
        sig = math.sin(2 * math.pi * freq * t) * env * 0.4
        samples.append((sig, sig))
    return samples

def make_warn_buzz(duration=0.55):
    """Amber conflict warning buzz."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 5.0)
        # Dual detuned saw waves
        saw1 = (2.0 * ((180.0 * t) % 1.0) - 1.0) * 0.5
        saw2 = (2.0 * ((186.0 * t) % 1.0) - 1.0) * 0.5
        sig = (saw1 + saw2) * env * 0.4
        samples.append((sig, sig))
    return samples

def make_resolved_bell(duration=0.9):
    """Warm major triad resolution chime."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    # D major triad: D5 (587.33), F#5 (739.99), A5 (880.0)
    chord = [587.33, 739.99, 880.0]
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 4.0)
        sig = sum(0.33 * math.sin(2 * math.pi * f * t) for f in chord) * env * 0.5
        samples.append((sig, sig))
    return samples

def make_decay_sweep(duration=0.8):
    """Downward filter sweep for TTL decay."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 3.5)
        freq = 600.0 * math.exp(-t * 3.0) + 80.0
        sig = math.sin(2 * math.pi * freq * t) * env * 0.35
        samples.append((sig, sig))
    return samples

def make_shield_deflect(duration=0.4):
    """High-Q metallic shield bounce ping."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        env = math.exp(-t * 16.0)
        metallic = (
            0.5 * math.sin(2 * math.pi * 2200.0 * t) +
            0.3 * math.sin(2 * math.pi * 3850.0 * t) +
            0.2 * math.sin(2 * math.pi * 5400.0 * t)
        )
        samples.append((metallic * env * 0.45, metallic * env * 0.45))
    return samples

def make_riser_swell(duration=3.2):
    """Cinematic riser building tension into finale."""
    n_samples = int(duration * SAMPLE_RATE)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Exponential curve up
        progress = t / duration
        env = math.pow(progress, 2.0)
        freq = 80.0 + 380.0 * math.pow(progress, 2.2)
        sine = math.sin(2 * math.pi * freq * t) * 0.4
        noise = (random.random() * 2 - 1) * 0.15 * progress
        sig = (sine + noise) * env * 0.55
        samples.append((sig, sig))
    return samples

def generate_all(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    write_wav(os.path.join(out_dir, "ambient_drone.wav"), make_ambient_drone(94.0))
    write_wav(os.path.join(out_dir, "mark_node_ping.wav"), make_ping(1760.0))
    write_wav(os.path.join(out_dir, "connection_glitch.wav"), make_glitch(0.45))
    write_wav(os.path.join(out_dir, "impact_boom.wav"), make_impact_boom(1.8))
    write_wav(os.path.join(out_dir, "whoosh_fast.wav"), make_whoosh(0.65))
    write_wav(os.path.join(out_dir, "key_click.wav"), make_key_click(0.07))
    write_wav(os.path.join(out_dir, "key_return.wav"), make_key_return(0.12))
    write_wav(os.path.join(out_dir, "crystallize.wav"), make_crystallize(0.85))
    write_wav(os.path.join(out_dir, "sonar_pulse.wav"), make_sonar_pulse(1.2))
    
    # 5 pentatonic chimes for 5-stage search
    pentatonic = [523.25, 587.33, 659.25, 783.99, 880.00]
    for idx, f in enumerate(pentatonic, 1):
        write_wav(os.path.join(out_dir, f"stage_chime_{idx}.wav"), make_stage_chime(f, 0.7))
        
    write_wav(os.path.join(out_dir, "packet_transmit.wav"), make_packet_transmit(0.25))
    write_wav(os.path.join(out_dir, "warn_buzz.wav"), make_warn_buzz(0.55))
    write_wav(os.path.join(out_dir, "resolved_bell.wav"), make_resolved_bell(0.9))
    write_wav(os.path.join(out_dir, "decay_filter.wav"), make_decay_sweep(0.8))
    write_wav(os.path.join(out_dir, "shield_deflect.wav"), make_shield_deflect(0.4))
    write_wav(os.path.join(out_dir, "riser_swell.wav"), make_riser_swell(3.2))
    print("All sound effects generated successfully!")

if __name__ == "__main__":
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else "./sfx"
    generate_all(dest)
