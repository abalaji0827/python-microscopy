#!/usr/bin/python

###############
# picoscope.py
#
# Basic driver for PicoScope oscilloscopes using the psospa driver (PicoScope 3000E and 5000E series),
# via Pico Technology's pyPicoSDK package (pip install pypicosdk). Tested on a PicoScope 3416E.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
################
import threading

import numpy as np
import pypicosdk as psdk


class PicoScope(object):
    def __init__(self, name='PicoScope', sample_rate=10e3, num_samples=5000):
        """

        Parameters
        ----------
        name: str
            Name of the scope
        sample_rate: float
            Initial sample rate in samples per second. The scope uses the nearest rate it supports.
        num_samples: int
            Initial number of samples per capture. Capture length is num_samples / sample_rate seconds.
        """
        self.name = name
        self.lock = threading.Lock()

        self.scope = psdk.psospa()
        self.scope.open_unit()
        self.serial = self.scope.get_unit_serial()

        self.channel_ranges = {}  # enabled channels, e.g. {'A': 'V5'}
        self.trigger = None  # (channel, threshold) or None for free-running captures
        self.sample_rate = float(sample_rate)
        self.num_samples = int(num_samples)

    def GetName(self):
        return self.name

    def AvailableRanges(self):
        """
        Returns
        -------
        ranges: list
            Names of the voltage ranges this version of pyPicoSDK accepts, e.g. 'V1', 'V5'
        """
        return [r.name for r in psdk.RANGE]

    def SetChannel(self, channel, range_name):
        """
        Enable a channel with a given voltage range.

        Parameters
        ----------
        channel: str
            Channel letter, 'A' to 'D'
        range_name: str
            Voltage range name, e.g. 'V5' for +-5 V. See AvailableRanges().
        """
        channel = channel.upper()
        try:
            rng = getattr(psdk.RANGE, range_name)
        except AttributeError:
            raise ValueError('Unknown range %r, expected one of %s' % (range_name, self.AvailableRanges()))

        with self.lock:
            self.scope.set_channel(channel=getattr(psdk.CHANNEL, channel), range=rng)
        self.channel_ranges[channel] = range_name

    def SetTrigger(self, channel, threshold):
        """
        Start each capture on a rising edge through threshold on the given channel.
        Without a trigger, captures start immediately.

        Parameters
        ----------
        channel: str
            Channel letter, 'A' to 'D'. Must be enabled with SetChannel first.
        threshold: float
            Trigger level. FIXME - confirm the units pyPicoSDK uses for this (mV in earlier versions).
        """
        channel = channel.upper()
        with self.lock:
            self.scope.set_simple_trigger(channel=getattr(psdk.CHANNEL, channel), threshold=threshold)
        self.trigger = (channel, threshold)

    def SetSampleRate(self, sample_rate):
        """

        Parameters
        ----------
        sample_rate: float
            Samples per second. The scope uses the nearest rate it supports.
        """
        self.sample_rate = float(sample_rate)

    def GetSampleRate(self):
        return self.sample_rate

    def SetNumSamples(self, num_samples):
        """

        Parameters
        ----------
        num_samples: int
            Samples per capture, per channel
        """
        self.num_samples = int(num_samples)

    def GetNumSamples(self):
        return self.num_samples

    def Capture(self):
        """
        Capture one block of data on all enabled channels.

        Returns
        -------
        t: np.ndarray
            Time axis, in the units returned by pyPicoSDK (ns in current versions)
        data: dict
            Channel letter -> np.ndarray of readings in mV, e.g. data['A']
        """
        if len(self.channel_ranges) == 0:
            raise RuntimeError('%s: no channels enabled - call SetChannel first' % self.name)

        with self.lock:
            timebase = self.scope.interval_to_timebase(interval=1. / self.sample_rate, unit=psdk.TIME_UNIT.S)
            data, t = self.scope.run_simple_block_capture(timebase, self.num_samples)

        return np.asarray(t), {ch.name: np.asarray(v) for ch, v in data.items()}

    def register(self, scope):
        self.registerStateHandlers(scope.state)

    def registerStateHandlers(self, scopeState):
        key = 'Oscilloscopes.%s.' % self.name
        scopeState.registerHandler(key + 'SampleRate', self.GetSampleRate, self.SetSampleRate)
        scopeState.registerHandler(key + 'NumSamples', self.GetNumSamples, self.SetNumSamples)

    def Close(self):
        print('Shutting down %s' % self.name)
        with self.lock:
            self.scope.close_unit()

    def __del__(self):
        try:
            self.Close()
        except Exception:
            pass