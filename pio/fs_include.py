"""PlatformIO pre-script: the core's USB library includes "FS.h", but the
library finder doesn't hand it the FS library's include path in the hybrid
(custom sdkconfig) build. Add it globally so every library sees it."""
from os.path import join

Import("env")  # noqa: F821 - provided by PlatformIO/SCons

fw = env.PioPlatform().get_package_dir("framework-arduinoespressif32")  # noqa: F821
env.Append(CPPPATH=[join(fw, "libraries", "FS", "src")])  # noqa: F821
