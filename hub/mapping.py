"""Drive mapping shared by the glove console and the hub service."""


def scale_axis(angle, deadzone, full_scale):
    """Angle in degrees -> -1..1 with deadzone."""
    mag = abs(angle)
    if mag <= deadzone:
        return 0.0
    frac = min(1.0, (mag - deadzone) / (full_scale - deadzone))
    return frac if angle > 0 else -frac


def throttle_from_tilt(tilt_back, brake_start, brake_full):
    """Neutral = full cruise; tilting back brakes progressively to 0."""
    if tilt_back <= brake_start:
        return 1.0
    if tilt_back >= brake_full:
        return 0.0
    return 1.0 - (tilt_back - brake_start) / (brake_full - brake_start)


def mix(throttle, steer, max_speed, min_pwm):
    """throttle/steer in -1..1 -> (left, right) PWM with stall floor."""
    left = throttle + steer
    right = throttle - steer
    biggest = max(1.0, abs(left), abs(right))
    left, right = left / biggest, right / biggest

    def to_pwm(v):
        if abs(v) < 1e-3:
            return 0
        span = max_speed - min_pwm
        return int((min_pwm + span * abs(v)) * (1 if v > 0 else -1))

    return to_pwm(left), to_pwm(right)


def v_omega_to_pwm(v, omega, max_speed, min_pwm):
    """Normalized v/omega in -1..1 -> (left, right) PWM."""
    return mix(v, omega, max_speed, min_pwm)
