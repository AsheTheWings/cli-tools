"""
Beep sound generator command.

This module provides a CLI command to generate periodic beep sounds
for a specified duration at maximum volume.
"""

import sys
import time

import click


def beep_max_volume():
    """Play beep at max volume, then restore original volume."""
    import winsound
    from pycaw.pycaw import AudioUtilities

    # Get default speakers device
    device = AudioUtilities.GetSpeakers()
    volume = device.EndpointVolume

    # Save current volume (scalar 0.0-1.0)
    original_volume = volume.GetMasterVolumeLevelScalar()

    try:
        # Set to max volume
        volume.SetMasterVolumeLevelScalar(1.0, None)

        # Beep (frequency=1000Hz, duration=300ms)
        winsound.Beep(1000, 300)

    finally:
        # Restore original volume
        volume.SetMasterVolumeLevelScalar(original_volume, None)


@click.command()
@click.argument("interval", type=int)
@click.argument("duration", type=int)
def beep_command(interval: int, duration: int) -> None:
    """
    Generate periodic beep sounds at maximum volume.

    INTERVAL: Time between beeps in minutes
    DURATION: Total duration in minutes

    Examples:
        tool beep 1 30    # Beep every 1 minute for 30 minutes
        tool beep 5 60    # Beep every 5 minutes for 1 hour
    """
    if interval <= 0:
        click.echo("❌ Interval must be positive", err=True)
        sys.exit(1)

    if duration <= 0:
        click.echo("❌ Duration must be positive", err=True)
        sys.exit(1)

    if interval > duration:
        click.echo("❌ Interval cannot be greater than duration", err=True)
        sys.exit(1)

    interval_seconds = interval * 60
    duration_seconds = duration * 60

    click.echo(f"🔔 Beeping every {interval} min for {duration} min (Ctrl+C to stop)")

    start_time = time.time()
    beep_count = 0

    try:
        while True:
            elapsed = time.time() - start_time
            if elapsed >= duration_seconds:
                break

            # Beep at max volume
            beep_max_volume()
            beep_count += 1

            remaining_total = duration_seconds - elapsed
            click.echo(
                f"  Beep #{beep_count} | {remaining_total / 60:.1f} min remaining"
            )

            # Countdown to next beep
            wait_time = min(interval_seconds, remaining_total)
            if wait_time > 0:
                countdown_end = time.time() + wait_time
                while True:
                    now = time.time()
                    if now >= countdown_end:
                        break
                    secs_left = int(countdown_end - now)
                    mins, secs = divmod(secs_left, 60)
                    click.echo(f"\r  Next beep in: {mins:02d}:{secs:02d}  ", nl=False)
                    time.sleep(1)
                click.echo("\r" + " " * 30 + "\r", nl=False)  # Clear line

    except KeyboardInterrupt:
        click.echo("\n⏹️  Stopped")

    click.echo(f"✅ Done. Total beeps: {beep_count}")


if __name__ == "__main__":
    beep_command()
