#!/usr/bin/env python3
"""
HyperFx - The bridge between Hyper-NG and LedFx
"""
import os
import sys
import json
import time
import asyncio
import websockets

import requests

import webcolors

from loguru import logger

from dotenv import load_dotenv

# Configure logging based on DEBUG environment variable
def setup_logging():
    """Configure loguru logging based on DEBUG environment variable."""
    # Remove default handler
    logger.remove()

    # Get debug setting from environment
    debug_enabled = os.environ.get('DEBUG', 'false').lower() == 'true'

    if debug_enabled:
        # Debug mode: show all messages with detailed format
        logger.add(
            sys.stdout,
            format=("<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                    "<level>{level: <8}</level> | "
                    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                    "<level>{message}</level>"),
            level="DEBUG",
            enqueue=True
        )
    else:
        # Production mode: show only important messages with simple format
        logger.add(
            sys.stdout,
            format="<level>{message}</level>",
            level="INFO",
            enqueue=True
        )

class RobustLedFxSync:
    """LedFx Sync Implementation with break protection."""

    def __init__(self):
        self.logger = logger.bind(name="RobustLedFxSync")

        # LedFx Settings
        self.ledfx_host = os.environ.get('LEDFX_HOST')
        self.ledfx_port = os.environ.get('LEDFX_PORT', '8888')
        self.ledfx_instance = os.environ.get('LEDFX_INSTANCE', 'hyperfx')

        # Hyperion-NG Settings
        self.hyperion_host = os.environ.get('HYPERION_HOST')
        self.hyperion_port = os.environ.get('HYPERION_PORT', '8090')

        # Validate required configuration
        if not self.hyperion_host or not self.ledfx_host:
            self.logger.error("HYPERION_HOST and LEDFX_HOST must be set in .env")
            raise SystemExit(1)

        # Set Hyperion-NG Websocket URL
        self.ws_url = f"ws://{self.hyperion_host}:{self.hyperion_port}/"

        # Load color samples and refresh rate from environment variable
        try:
            self.refresh_rate = float(os.environ.get('REFRESH_RATE', '0.1'))
            # Ensure minimum and maximum bounds
            self.refresh_rate = max(0.01, min(self.refresh_rate, 2.0))  # 10ms min, 2s max

            # Set color samples
            self.color_samples = int(os.environ.get('COLOR_SAMPLES', '3'))

            self.logger.debug(f"🎯 HyperFx initialized - Color samples: {self.color_samples} - "
                              f"Refresh rate: {self.refresh_rate}s ({1/self.refresh_rate:.1f} FPS)")
        except (ValueError, TypeError):
            self.refresh_rate = 0.1  # Default refresh rate fallback
            self.color_samples = 10  # Default color samples fallback
            self.logger.warning("⚠️  Invalid refresh rate, using defaults: "
                                 f"Color Samples: {self.color_samples}, "
                                 f"Refresh Rate: {self.refresh_rate}s")

        # Health monitoring
        self.health_status = {
            'hyperion_connected': False,
            'ledfx_connected': False,
            'last_sync': None,
            'sync_count': 0,
            'error_count': 0,
            'last_error': None
        }
        self.last_health_report = 0

    async def _health_check(self):
        """Periodic health check of system status."""
        try:
            # Test Hyperion connection
            hyperion_test = requests.get(
                f"http://{self.hyperion_host}:{self.hyperion_port}",
                json={"command":"serverinfo", "instance": 1}, timeout=5)
            self.health_status['hyperion_connected'] = hyperion_test.status_code == 200

            # Test LedFx connection
            ledfx_test = requests.get(
                f"http://{self.ledfx_host}:{self.ledfx_port}/api/virtuals", timeout=5)
            self.health_status['ledfx_connected'] = ledfx_test.status_code == 200

            # Report status
            status_emoji = "✅" if (self.health_status['hyperion_connected'] and
                                   self.health_status['ledfx_connected']) else "⚠️"
            sync_count = self.health_status['sync_count']
            error_count = self.health_status['error_count']

            self.logger.info(f"{status_emoji} | Health: {sync_count} syncs, {error_count} errors | "
                             f"Hyperion: {self.health_status['hyperion_connected']} | "
                             f"LedFx: {self.health_status['ledfx_connected']}")

            # Reset error count after reporting
            if self.health_status['error_count'] > 0:
                self.health_status['error_count'] = 0

        except Exception as e:
            self.logger.warning(f"⚠️  Health check failed: {e}")
            self.health_status['hyperion_connected'] = False
            self.health_status['ledfx_connected'] = False

    async def color_name(self, color_rgb):
        """Get color name from RGB value"""
        if not color_rgb:
            logger.error("Got empty value for color_rgb")
            return None

        min_colors = {}
        for key, name in webcolors._definitions._CSS3_HEX_TO_NAMES.items():
            r_c, g_c, b_c = webcolors.hex_to_rgb(key)
            rd = (r_c - color_rgb[0]) ** 2
            gd = (g_c - color_rgb[1]) ** 2
            bd = (b_c - color_rgb[2]) ** 2
            min_colors[(rd + gd + bd)] = name

        color_name = min_colors[min(min_colors.keys())]
        return color_name

    async def robust_sync_functional(self):
        """Robust error handling, no breaks."""

        self.logger.debug("🎯 Starting color synchronization...")

        color_result = None

        try:
            self.logger.debug("🔌 Connecting to Hyperion WebSocket...")
            async with websockets.connect(self.ws_url) as websocket:
                self.logger.debug("✅ LedFx WebSocket connection successfully")

                # Start streaming - functional approach
                start_cmd = {
                    "command": "ledcolors",
                    "subcommand": "ledstream-start",
                     "instance": 1,  # TODO: Make more dynamic solution for Hyperion
                    "tan": 1
                }

                await websocket.send(json.dumps(start_cmd) + "\n")
                self.logger.debug("📤 Sent: LedFx ledstream-start")

                # Wait for confirmation with robust handling
                try:
                    response = await asyncio.wait_for(websocket.recv(), timeout=8.0)
                    result = json.loads(response)
                    if result.get('success'):
                        self.logger.debug("✅ Hyperion Streaming connection successfully")
                        color_result = await self.robust_capture_colors(websocket)

                        if color_result:
                            color_rgb = color_result['rgb']
                            color_name = await self.color_name(color_rgb)
                            return color_result
                    else:
                        self.logger.warning("⚠️  Streaming confirmation failed")

                except asyncio.TimeoutError:
                    self.logger.warning("⏰ WebSocket confirmation timeout - continuing anyway")
                    # Continue and try to capture
                    color_result = await self.robust_capture_colors(websocket)

                except json.JSONDecodeError as e:
                    self.logger.error(f"🔧 JSON decode error - continuing: {e}")
                    color_result = await self.robust_capture_colors(websocket)

                except Exception as e:
                    self.logger.error(f"🔧 General WebSocket error - recovering: {e}")
                    # Try to continue with capture
                    color_result = await self.robust_capture_colors(websocket)

            # Continue even if WebSocket issues
            if not color_result:
                self.logger.warning("🔧 WebSocket completed - attempting final capture anyway")
                # Try final connection method
                color_result = await self.final_robust_capture()

            return color_result

        except websockets.exceptions.WebSocketException as e:
            self.logger.error(f"💥 WebSocket connection error: {e}")
            # Try direct API approach
            return await self.direct_api_approach()

        except Exception as e:
            self.logger.error(f"💥 Complete system error: {e}")
            return await self.direct_api_approach()

    async def robust_capture_colors(self, websocket):
        """CAPTURE - Robust message handling with no breaks."""

        colors_captured = []
        start_time = time.time()
        max_duration = 8.0  # Reasonable timeout
        sample_count = 0
        best_color = None

        self.logger.debug("👀 Starting color extraction...")

        while sample_count < self.color_samples and (time.time() - start_time) < max_duration:
            try:
                # Robust message reception
                message = await asyncio.wait_for(websocket.recv(), timeout=10.0)

                # Robust extraction
                color_data = self.robust_extract_color(message)

                if color_data:
                    colors_captured.append(color_data)
                    sample_count += 1

                    # Show progress in debug mode only
                    current_rgb = color_data['rgb']
                    color_name = await self.color_name(current_rgb)
                    self.logger.debug(f"📊 Sample {sample_count}: {color_name} "
                                      f"({current_rgb[0]}, {current_rgb[1]}, {current_rgb[2]})")

                    # Calculate running average safely
                    if len(colors_captured) >= 3:
                        best_color = self.robust_calculate_average(colors_captured)
                        color_name = await self.color_name(best_color)
                        self.logger.debug(f"🎯 Running avg: {color_name} "
                                          f"({best_color[0]}, {best_color[1]}, {best_color[2]})")

            except asyncio.TimeoutError:
                # Handle timeouts without breaking
                self.logger.debug(".")  # Progress without breaking
                continue

            except Exception as e:
                self.logger.error(f"🔧 Processing error - recovering: {e}")
                continue  # Keep going

        if best_color:
            color_name = await self.color_name(best_color)
            self.logger.debug(f"🎯 Color: {color_name} ({best_color[0]}, {best_color[1]}, "
                              f"{best_color[2]})")
            return {'rgb': best_color, 'led_count': len(colors_captured),
                    'source': 'robust_capture'}

        return None

    def robust_extract_color(self, message):
        """EXTRACT - Bulletproof color extraction."""

        try:
            data = json.loads(message)

            # Look for color data in any format - using string detection to avoid type issues
            data_str = str(data).lower()
            if 'leds' in data_str or 'color' in data_str:
                # Multiple extraction approaches
                led_data = data.get('data', {}).get('leds', [])

                if led_data and len(led_data) >= 3 and len(led_data) % 3 == 0:
                    # Safe RGB extraction with validation
                    rgb_groups = []
                    for i in range(0, len(led_data), 3):
                        rgb = led_data[i:i+3]
                        # Ensure all elements are numbers
                        if len(rgb) == 3 and all(isinstance(x, (int, float)) for x in rgb):
                            rgb_groups.append(rgb)

                    if rgb_groups:  # Only proceed with valid data
                        # Fast average calculation
                        try:
                            r_total = sum(rgb[0] for rgb in rgb_groups)
                            g_total = sum(rgb[1] for rgb in rgb_groups)
                            b_total = sum(rgb[2] for rgb in rgb_groups)

                            avg_r = int(r_total / len(rgb_groups))
                            avg_g = int(g_total / len(rgb_groups))
                            avg_b = int(b_total / len(rgb_groups))

                            return {
                                'rgb': [avg_r, avg_g, avg_b],
                                'led_count': len(rgb_groups),
                                'source': 'robust_extraction',
                                'status': 'complete'
                            }
                        except (ZeroDivisionError, IndexError) as e:
                            self.logger.error(f"🔧 RGB extraction error - using backup: {e}")
                            return None

            return None

        except Exception as e:
            self.logger.error(f"🔧 Robust extraction error - handling: {e}")
            return None

    def robust_calculate_average(self, colors):
        """AVERAGE - Bulletproof calculation without the str index error."""

        if not colors:
            return None

        # Ensure colors are properly formatted dictionaries
        valid_colors = []
        for color in colors:
            try:
                if (isinstance(color, dict) and 'rgb' in color and
                    isinstance(color['rgb'], (list, tuple)) and len(color['rgb']) == 3):
                    valid_colors.append(color)
                elif isinstance(color, (list, tuple)) and len(color) == 3:
                    # Handle case where color might be a list directly
                    valid_colors.append({'rgb': color})
            except Exception as e:
                # Skip invalid color data silently
                self.logger.warning(e)
                continue

        if not valid_colors:
            return None

        # Weighted average for quality
        weights = list(range(1, len(valid_colors) + 1))
        total_weight = sum(weights)

        try:
            r_avg = int(sum(c['rgb'][0] * weights[i] for i, c in enumerate(valid_colors)) /
                        total_weight)
            g_avg = int(sum(c['rgb'][1] * weights[i] for i, c in enumerate(valid_colors)) /
                        total_weight)
            b_avg = int(sum(c['rgb'][2] * weights[i] for i, c in enumerate(valid_colors)) /
                        total_weight)

            result = [r_avg, g_avg, b_avg]
            return result

        except (ZeroDivisionError, IndexError, TypeError, KeyError) as e:
            self.logger.warning(e)
            # Fallback calculation
            try:
                flat_average = [
                    int(sum(c['rgb'][0] for c in valid_colors) / len(valid_colors)),
                    int(sum(c['rgb'][1] for c in valid_colors) / len(valid_colors)),
                    int(sum(c['rgb'][2] for c in valid_colors) / len(valid_colors))
                ]
                return flat_average
            except Exception as e:
                self.logger.warning(e)
                return None

    async def final_robust_capture(self):
        """FINAL BACKUP - Direct API if WebSocket fails."""

        self.logger.debug("🔧 Using direct API approach (backup method)")

        try:
            # Use direct REST API as backup
            response = requests.get(
                f"http://{self.hyperion_host}:{self.hyperion_port}/json-rpc",
                json={"command":"serverinfo", "instance": 1}, timeout=10)

            if response.status_code == 200:
                data = response.json()
                info = data.get('info', {})
                active_colors = info.get('activeLedColor', [])

                if active_colors:
                    self.logger.debug("✅ Direct API success!")

                    # Safe extraction
                    rgb_values = []
                    for color in active_colors:
                        if isinstance(color, dict) and 'RGB Value' in color:
                            rgb = color['RGB Value']
                            if len(rgb) >= 3:
                                rgb_values.append(rgb[:3])

                    if rgb_values:
                        avg_rgb = [
                            int(sum(rgb[0] for rgb in rgb_values) / len(rgb_values)),
                            int(sum(rgb[1] for rgb in rgb_values) / len(rgb_values)),
                            int(sum(rgb[2] for rgb in rgb_values) / len(rgb_values))
                        ]

                        return {
                            'rgb': avg_rgb,
                            'led_count': len(rgb_values),
                            'source': 'direct_api_backup',
                            'status': 'direct_api'
                        }

            return None

        except Exception as e:
            self.logger.error(f"🔧 Direct API failed: {e}")
            return None

    async def direct_api_approach(self):
        """DIRECT - Pure REST API method."""

        self.logger.debug("🔧 DIRECT API METHOD - Most reliable approach")

        return await self.final_robust_capture()

    async def send_to_ledfx(self, rgb_color):
        """Send color to LedFx side-lights device."""

        try:
            ledfx_url = f"http://{self.ledfx_host}:{self.ledfx_port}"

            # Ensure rgb_color is a list/tuple of 3 integers
            if isinstance(rgb_color, dict) and 'rgb' in rgb_color:
                rgb_values = rgb_color['rgb']
            elif isinstance(rgb_color, (list, tuple)) and len(rgb_color) >= 3:
                rgb_values = rgb_color[:3]
            else:
                self.logger.warning(f"⚠️  Invalid color format: {rgb_color}")
                return False

            # Apply brightness enhancement for dark indoor dance festival videos
            enhanced_rgb = self.enhance_dark_colors(rgb_values)

            # Minimum brightness floor for daylight visibility and audio reactivity
            # Ensures LEDs never go completely dark even during dark/silent scenes.
            # When enabled (MIN_BRIGHTNESS > 0), dim colors are scaled up preserving hue,
            # and pure black falls back to a warm-white floor so LedFx audio-reactive
            # effects always have visible signal to work with.
            min_brightness = int(os.environ.get('MIN_BRIGHTNESS', '0'))
            min_brightness = max(0, min(min_brightness, 255))

            if min_brightness > 0:
                max_channel = max(enhanced_rgb)

                if max_channel == 0:
                    # Pure black - use warm-white floor at min_brightness level
                    # This preserves audio reactivity during completely dark scenes
                    enhanced_rgb = [
                        min_brightness,
                        min_brightness,
                        min_brightness // 2
                    ]
                    self.logger.debug(f"🌑 Black floor (warm white): "
                                      f"{rgb_values} → {enhanced_rgb}")

                elif max_channel < min_brightness:
                    # Dim but has color signal - scale preserving hue
                    scale = min_brightness / max_channel
                    enhanced_rgb = [
                        min(255, int(enhanced_rgb[0] * scale)),
                        min(255, int(enhanced_rgb[1] * scale)),
                        min(255, int(enhanced_rgb[2] * scale))
                    ]

            # Last-resort safety: never send pure black to LedFx
            # This ensures audio-reactive effects stay visible even when MIN_BRIGHTNESS=0
            # or when enhancement produces zero output from pure black input
            if enhanced_rgb[0] == 0 and enhanced_rgb[1] == 0 and enhanced_rgb[2] == 0:
                enhanced_rgb = [3, 3, 6]  # barely visible warm dim
                self.logger.debug(f"🌑 Safety floor (always-on): {rgb_values} → {enhanced_rgb}")

            # Convert RGB to hex color for LedFx
            hex_color = f"#{enhanced_rgb[0]:02x}{enhanced_rgb[1]:02x}{enhanced_rgb[2]:02x}"

            # Use energy effect which was successful in our tests
            brightness = os.environ.get('BRIGHTNESS', '1.0')
            color_data = {
                "type": "energy",
                "config": {
                    "color_high": hex_color,
                    "color_mids": hex_color,
                    "color_lows": hex_color,
                    "brightness": float(brightness),
                    "background_brightness": 0.1,
                    "intensity": 1.0
                }
            }

            response = requests.post(
                f"{ledfx_url}/api/virtuals/{self.ledfx_instance}/effects",
                json=color_data,
                timeout=5
            )

            if response.status_code == 200:
                color_name = await self.color_name(enhanced_rgb)
                if enhanced_rgb != rgb_values:
                    self.logger.success(f"✅ LedFx updated: {color_name} (enhanced: {rgb_values} → {enhanced_rgb})")
                else:
                    self.logger.success(f"✅ LedFx updated: {color_name} ({enhanced_rgb})")
                return True

            self.logger.warning(f"⚠️  LedFx response ({response.status_code}): "
                                f"{response.text}")
            return False

        except Exception as e:
            self.logger.error(f"🔧 LedFx send error: {e}")
            return False

    def enhance_dark_colors(self, rgb_values):
        """Enhance dark colors to prevent LEDs from going too dark for indoor dance festivals.
        
        This method brightens colors that are below a threshold, which is particularly useful
        for dark indoor dance festival videos where colors might be too dim for LED effects.
        
        Args:
            rgb_values: List/tuple of RGB values [R, G, B] (0-255)
            
        Returns:
            List of enhanced RGB values [R, G, B] (0-255)
        """
        try:
            # Load configuration from environment with defaults
            enhance_enabled = os.environ.get('BRIGHTNESS_ENHANCE_ENABLE', 'true').lower() == 'true'
            if not enhance_enabled:
                return rgb_values
            
            # Get enhancement parameters from environment
            threshold = int(os.environ.get('BRIGHTNESS_ENHANCE_THRESHOLD', '80'))
            factor = float(os.environ.get('BRIGHTNESS_ENHANCE_FACTOR', '1.5'))
            
            # Ensure parameters are within valid ranges
            threshold = max(0, min(threshold, 255))
            factor = max(1.0, min(factor, 3.0))
            
            # Check if color is dark enough to need enhancement
            max_component = max(rgb_values[0], rgb_values[1], rgb_values[2])
            
            if max_component <= threshold:
                # Calculate enhancement - proportional to how dark the color is
                # Darker colors get more enhancement
                darkness_ratio = (threshold - max_component) / threshold
                current_factor = 1.0 + (darkness_ratio * (factor - 1.0))
                
                # Apply enhancement to all color channels proportionally
                enhanced_r = min(255, int(rgb_values[0] * current_factor))
                enhanced_g = min(255, int(rgb_values[1] * current_factor))
                enhanced_b = min(255, int(rgb_values[2] * current_factor))
                
                enhanced_color = [enhanced_r, enhanced_g, enhanced_b]
                
                self.logger.debug(f"🌟 Color enhanced: {rgb_values} → {enhanced_color} "
                                f"(factor: {current_factor:.2f}, threshold: {threshold})")
                return enhanced_color
            
            # Color is bright enough, return as-is
            return rgb_values
            
        except (ValueError, TypeError, IndexError) as e:
            self.logger.warning(f"⚠️  Brightness enhancement error (using original): {e}")
            return rgb_values

    async def continuous_sync_loop(self):
        """Continuous real-time sync loop with health monitoring."""

        self.logger.info(f"🎯 HyperFx active ({self.color_samples}cs - {1/self.refresh_rate:.0f}fps) - "
                         "Press Ctrl+C to stop")
        last_color = None
        update_count = 0
        health_check_interval = 60  # Check health every 60 seconds
        last_health_check = time.time()

        while True:
            try:
                # Perform color sync
                color_result = await self.robust_sync_functional()

                if color_result:
                    # Handle both dictionary and list return types
                    if isinstance(color_result, dict) and 'rgb' in color_result:
                        current_color = color_result['rgb']
                        color_name = await self.color_name(current_color)
                    elif isinstance(color_result, list) and len(color_result) >= 3:
                        current_color = color_result
                        color_name = await self.color_name(current_color)
                    else:
                        current_color = None
                        color_name = None

                    if current_color and len(current_color) >= 3:
                        # Only update LedFx if color changed (reduces unnecessary updates)
                        if current_color != last_color:
                            success = await self.send_to_ledfx(color_result)

                            if success:
                                update_count += 1
                                self.health_status['sync_count'] = update_count
                                self.health_status['last_sync'] = time.time()
                                # Only log color changes every 10 updates to reduce spam by default
                                if update_count % 10 == 0:
                                    self.logger.debug(f"🔄 Sync #{update_count}: {color_name} "
                                              f"(RGB{current_color})")

                            last_color = current_color

                        # Add small delay based on refresh rate
                        await asyncio.sleep(max(0.05, self.refresh_rate))  # Minimum 50ms delay

                # Health check every 60 seconds
                current_time = time.time()
                if current_time - last_health_check >= health_check_interval:
                    await self._health_check()
                    last_health_check = current_time

            except KeyboardInterrupt:
                self.logger.info("🛑 Stopped by user")
                break

            except Exception as e:
                self.logger.error(f"💥 Sync error: {e}")
                self.health_status['error_count'] += 1
                self.health_status['last_error'] = time.time()
                await asyncio.sleep(max(1.0, self.refresh_rate * 5))  # Recovery delay

        self.logger.info("⏹️  HyperFx shutdown complete")

async def robust_functional_sync():
    """FUNCTIONAL - One-time sync test."""

    logger.debug("🎬 Testing synchronization...")

    sync = RobustLedFxSync()
    result = await sync.robust_sync_functional()

    if result and result['rgb']:
        logger.success(f"✅ Sync test passed: RGB{result['rgb']}")
        return result

    logger.error("💥 Sync test failed")
    return None

async def main():
    """Main entry point."""

    # Load .env before configuring logging so DEBUG var is available
    load_dotenv()
    setup_logging()

    sync = RobustLedFxSync()
    await sync.continuous_sync_loop()

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.warning("⏹️  Functional sync stopped by user")
    except Exception as e:
        logger.exception(f"💥 Functional sync failed: {e}")
        logger.error("Alternative integration methods needed")
