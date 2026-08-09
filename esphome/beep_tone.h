#pragma once

#include <cmath>
#include <cstdint>
#include <vector>
#include "esphome/components/speaker/speaker.h"

// Minimal sine tone, written directly to a speaker component. Amplitude
// defaults well under full scale and is never boosted past it: a clipped or
// square wave carries much more continuous power than a clean sine at the
// same peak, which is harsher on this board's tiny amp/speaker and risks
// Damages
namespace beep_tone {

inline void play_tone(esphome::speaker::Speaker *spk, float frequency_hz, uint32_t duration_ms,
                       uint32_t sample_rate = 16000, int16_t amplitude = 20000) {
  spk->set_volume(1.0f);  // rule out any software attenuation left over from prior config/persisted state

  // Hard cap regardless of what the caller asks for so a mistaken
  // or runaway duration should never turn into a long continuous drive.
  // which could damage the hardware
  if (duration_ms > 300)
    duration_ms = 300;

  size_t num_samples = static_cast<size_t>(sample_rate) * duration_ms / 1000;

  // The speaker is configured with channel: stereo (this board's amp sounds
  // noticeably worse in the driver's native mono mode), which expects
  // pre-interleaved L/R pairs, so duplicate every sample to both channels.
  std::vector<int16_t> samples(num_samples * 2);
  for (size_t i = 0; i < num_samples; i++) {
    float t = static_cast<float>(i) / static_cast<float>(sample_rate);
    int16_t sample = static_cast<int16_t>(amplitude * sinf(2.0f * static_cast<float>(M_PI) * frequency_hz * t));
    samples[i * 2] = sample;
    samples[i * 2 + 1] = sample;
  }

  const auto *data = reinterpret_cast<const uint8_t *>(samples.data());
  size_t length = samples.size() * sizeof(int16_t);
  size_t written = 0;
  while (written < length) {
    size_t n = spk->play(data + written, length - written);
    if (n == 0) {
      delay(5);
    } else {
      written += n;
    }
  }
}

}  // namespace beep_tone
