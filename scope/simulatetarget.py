#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Simulate Target
---------------
Generate a forward model of a telescope detector with sensitivity variation,
and simulate stellar targets with motion relative to the CCD.

'''

import numpy as np
import matplotlib.pyplot as pl
from matplotlib.widgets import Button
import everest
from everest.mathutils import SavGol
from .scopemath import PSF, PLD
import random
from random import randint
from astropy.io import fits
from everest import Transit
import k2plr
from k2plr.config import KPLR_ROOT
from everest.config import KEPPRF_DIR
from everest.missions.k2 import CDPP
import os
from tqdm import tqdm
from datetime import datetime
from scipy.ndimage import zoom

# astroML format for consistent plotting style
from astroML.plotting import setup_text_plots
setup_text_plots(fontsize=10, usetex=True)


class Target(object):
    '''
    A simulated stellar object with a forward model of a telescope detector's sensitivity variation
    '''

    def __init__(self, ID=205998445, custom_ccd=False, transit=False, variable=False, neighbor=False, ftpf=None):

        # initialize self variables
        self.ID = ID
        self.ftpf = ftpf
        self.custom_ccd = custom_ccd
        self.transit = transit
        self.variable = variable
        self.neighbor = neighbor
        self.targets = 1

        self.startTime = datetime.now()

    def GenerateLightCurve(self, mag=12., roll=1., background_level=0., ccd_args=[], neighbor_magdiff=1., photnoise_conversion=.000625, ncadences=1000, apsize=7):
        '''
        Creates a light curve for given detector, star, and transiting exoplanet parameters
        Motion from a real K2 target is applied to the PSF

        `mag`: Magnitude of primary target PSF
        `roll`: Coefficient on K2 motion vectors of target. roll=1 corresponds to current K2 motion

        '''

        self.ncadences = ncadences
        self.t = np.linspace(0, 90, self.ncadences) # simulation lasts 90 days, with n cadences
        self.apsize = apsize # number of pixels to a side for aperture
        self.background_level = background_level
        self.aperture = np.ones((self.ncadences, self.apsize, self.apsize))

        # calculate PSF amplitude for given Kp Mag
        self.A = self.PSFAmplitude(mag)

        # read in K2 motion vectors for provided K2 target (EPIC ID #)
        if self.ftpf is None:

            # access target information
            client=k2plr.API()
            star=client.k2_star(self.ID)
            tpf=star.get_target_pixel_files(fetch = True)[0]
            ftpf=os.path.join(KPLR_ROOT, 'data', 'k2', 'target_pixel_files', '%d' % self.ID, tpf._filename)
        else:
            ftpf=self.ftpf
        with fits.open(ftpf) as f:

            # read motion vectors in x and y
            self.xpos=f[1].data['pos_corr1']
            self.ypos=f[1].data['pos_corr2']

        # throw out outliers
        for i in range(len(self.xpos)):
            if abs(self.xpos[i]) >= 50 or abs(self.ypos[i]) >= 50:
                self.xpos[i] = 0
                self.ypos[i] = 0
            if np.isnan(self.xpos[i]):
                self.xpos[i] = 0
            if np.isnan(self.ypos[i]):
                self.ypos[i] = 0

        # crop to desired length and multiply by roll coefficient
        self.xpos = self.xpos[0:self.ncadences] * roll
        self.ypos = self.ypos[0:self.ncadences] * roll

        # create self.inter-pixel sensitivity variation matrix
        # random normal distribution centered at 0.975
        self.inter = np.zeros((self.apsize, self.apsize))
        for i in range(self.apsize):
            for j in range(self.apsize):
                self.inter[i][j] = (0.975 + 0.001 * np.random.randn())

        # assign PSF model parameters to be passed into PixelFlux function
        if not self.custom_ccd:

            # cx,cy: intra-pixel variation polynomial coefficients in x,y
            self.cx = [1.0, 0.0, -0.05]
            self.cy = [1.0, 0.0, -0.05]

            # x0,y0: center of PSF, half of aperture size plus random deviation
            x0 = (self.apsize / 2.0) + 0.2 * np.random.randn()
            y0 = (self.apsize / 2.0) + 0.2 * np.random.randn()

            # sx,sy: standard deviation of Gaussian in x,y
            # rho: rotation angle between x and y dimensions of Gaussian
            sinx = np.linspace(0, 5*np.pi, self.ncadences) #hack
            sinvals = 2. + np.sin(sinx)
            sx = [0.5 + 0.05 * np.random.randn()]
            sy = [0.5 + 0.05 * np.random.randn()]
            rho = [0.05 + 0.02 * np.random.randn()]
            psf_args = np.concatenate([[self.A], np.array([x0]), np.array([y0]), sx, sy, rho])

        ccd_args = [self.cx, self.cy, self.apsize, background_level, self.inter, photnoise_conversion]
        self.ccd_args = ccd_args

        # initialize pixel flux light curve, target light curve, and isolated noise in each pixel
        self.fpix = np.zeros((self.ncadences, self.apsize, self.apsize))
        self.target = np.zeros((self.ncadences, self.apsize, self.apsize))
        self.ferr = np.zeros((self.ncadences, self.apsize, self.apsize))

        '''
        Here is where the light curves are created
        PSF function calculates flux in each pixel
        Iterate through cadences (c), and x and y dimensions on the detector (i,j)
        '''

        for c in tqdm(range(self.ncadences)):

            self.fpix[c], self.target[c], self.ferr[c] = PSF(psf_args, ccd_args, self.xpos[c], self.ypos[c])

        # add transit and variability
        if self.transit:
            self.fpix, self.flux = self.AddTransit()
        if self.variable:
            self.fpix, self.flux = self.AddVariability()
        if self.neighbor:
            self.fpix, self.flux = self.AddNeighbor()

        if not self.transit and not self.variable:
            # create flux light curve
            self.flux = np.sum(self.fpix.reshape((self.ncadences), -1), axis=1)

        return self.fpix, self.flux, self.ferr

    def Detrend(self, fpix=[]):
        '''
        Runs 2nd order PLD with a Gaussian Proccess on a given light curve
        '''

        # check if fpix light curve was passed in
        if len(fpix) == 0:
            fpix = self.fpix

        # Set empty transit mask if no transit provided
        if not self.transit:
            self.trninds = np.array([])

        # define aperture

        self.aperture = self.Aperture(fpix)


        # Run 2nd order PLD with a Gaussian Process
        flux, rawflux = PLD(fpix, self.ferr, self.trninds, self.t, self.aperture)

        self.detrended_cdpp = self.FindCDPP(flux)
        self.raw_cdpp = self.FindCDPP(rawflux)

        return flux, rawflux

    def PSFAmplitude(self, mag):
        '''
        Returns the amplitude of the PSF for a star of a given magnitude.
        '''

        # mag/flux relation constants
        a,b,c = 1.65e+07, 0.93, -7.35

        return a * np.exp(-b * (mag+c))


    def AddTransit(self, fpix=[], depth=.001, per=15, dur=.5, t0=5.):
        '''
        Injects a transit into light curve
        '''

        # check if fpix light curve was passed in
        if len(fpix) == 0:
            fpix = self.fpix

        self.transit = True

        # Transit information
        self.depth = depth
        self.per = per # period (days)
        self.dur = dur # duration (days)
        self.t0 = t0 # initial transit time (days)

        # Create transit light curve
        if self.depth == 0:
            self.trn = np.ones((self.ncadences))
        else:
            self.trn = Transit(self.t, t0=self.t0, per=self.per, dur=self.dur, depth=self.depth)

        # Define transit mask
        self.trninds = np.where(self.trn>1.0)
        self.M=lambda x: np.delete(x, self.trninds, axis=0)

        # Add transit to light curve
        self.fpix_trn = np.zeros((self.ncadences, self.apsize, self.apsize))
        for i,c in enumerate(fpix):
            self.fpix_trn[i] = c * self.trn[i]

        # Create flux light curve
        self.flux_trn = np.sum(self.fpix_trn.reshape((self.ncadences), -1), axis=1)

        self.fpix = self.fpix_trn
        self.flux = self.flux_trn

        return self.fpix_trn, self.flux_trn

    def AddVariability(self, fpix=[], var_amp=0.0005, freq=0.25, custom_variability=[]):
        '''
        Add a sinusoidal variability model to the given light curve.
        '''

        # check if fpix light curve was passed in
        if len(fpix) == 0:
            fpix = self.fpix

        self.variable = True

        # Check for custom variability
        if len(custom_variability) != 0:
            V = custom_variability
        else:
            V = 1 + var_amp * np.sin(freq*self.t)

        # Add variability to light curve
        V_fpix = [f * V[i] for i,f in enumerate(fpix)]

        # Create flux light curve
        V_flux = np.sum(np.array(V_fpix).reshape((self.ncadences), -1), axis=1)

        self.fpix = V_fpix
        self.flux = V_flux

        return V_fpix, V_flux

    def AddNeighbor(self, fpix=[], magdiff=1., dist=2.5):
        '''
        Add a neighbor star with given difference in magnitude and distance at a randomized location
        '''

        if len(fpix) == 0:
            fpix = self.fpix

        # initialize arrays
        n_fpix = np.zeros((self.ncadences, self.apsize, self.apsize))
        neighbor = np.zeros((self.ncadences, self.apsize, self.apsize))
        n_ferr = np.zeros((self.ncadences, self.apsize, self.apsize))

        # set neighbor params
        x_offset = dist * np.random.randn()
        y_offset = np.sqrt(dist**2 - x_offset**2) * random.choice((-1, 1))
        nx0 = (self.apsize / 2.0) + x_offset
        ny0 = (self.apsize / 2.0) + y_offset
        sx = [0.5 + 0.05 * np.random.randn()]
        sy = [0.5 + 0.05 * np.random.randn()]
        rho = [0.05 + 0.02 * np.random.randn()]

        neighbor_args = np.concatenate([[self.A], [nx0], [ny0], sx, sy, rho])

        # calculate comparison factor for neighbor, based on provided difference in magnitude
        self.r = 10 ** (magdiff / 2.5)

        # create neighbor pixel-level light curve
        for c in tqdm(range(self.ncadences)):

            # iterate through cadences, calculate pixel flux values
            n_fpix[c], neighbor[c], n_ferr[c] = PSF(neighbor_args, self.ccd_args, self.xpos[c], self.ypos[c])

            # divide by magdiff factor
            n_fpix[c] /= self.r
            neighbor[c] /= self.r

        # add neighbor to light curve
        fpix += n_fpix
        self.n_fpix = n_fpix

        # calculate flux light curve
        flux = np.sum(np.array(fpix).reshape((self.ncadences), -1), axis=1)

        self.neighbor = True
        self.targets += 1

        self.fpix = fpix
        self.flux = flux

        return fpix, flux

    def Aperture(self, fpix=[]):
        '''
        Create an aperture including all pixels containing target flux
        '''

        # check if fpix light curve was passed in
        if len(fpix) == 0:
            fpix = self.fpix

        aperture = np.zeros((self.ncadences, self.apsize, self.apsize))

        # Identify pixels with target flux for each cadence
        for c,f in enumerate(self.target):
            for i in range(self.apsize):
                for j in range(self.apsize):
                    if f[i][j] < 100.:
                        aperture[c][i][j] = 0
                    else:
                        aperture[c][i][j] = 1

        # Identify pixels with target flux for each cadence
        if self.neighbor:
            for c,f in enumerate(self.n_fpix):
                for i in range(self.apsize):
                    for j in range(self.apsize):
                        if f[i][j] > (.5 * np.max(f)):
                            aperture[c][i][j] = 0

        # Create single aperture
        finalap = np.zeros((self.apsize, self.apsize))

        # Sum apertures to weight pixels
        for i in range(self.apsize):
            for ap in aperture:
                finalap[i] += ap[i]

        max_counts = np.max(finalap)

        # Normalize to 1
        self.weighted_aperture = finalap / max_counts

        # Set excluded pixels to NaN
        for i in range(self.apsize):
            for j in range(self.apsize):
                if finalap[i][j] == 0:
                    finalap[i][j] = np.nan
                else:
                    finalap[i][j] = 1.

        self.aperture = finalap

        return finalap


    def DisplayAperture(self):
        '''
        Displays aperture overlaid over first cadence tpf
        '''

        self.Aperture()

        pl.imshow(self.fpix[0] * self.aperture, origin='lower', cmap='viridis', interpolation='nearest')

        pl.show()


    def DisplayDetector(self):
        '''
        Returns matrix for CCD pixel sensitivity
        '''

        # Define detector dimensions
        xdim = np.linspace(0, self.apsize, 100)
        ydim = np.linspace(0, self.apsize, 100)

        # Pixel resolution
        res = int(1000 / self.apsize)

        pixel_sens = np.zeros((res,res))

        # Calculate sensitivity function with detector parameters for individual pixel
        for i in range(res):
            for j in range(res):
                pixel_sens[i][j] = np.sum([c * (i-res/2) ** m for m, c in enumerate(self.cx)], axis = 0) + \
                np.sum([c * (j-res/2) ** m for m, c in enumerate(self.cy)], axis = 0)

        # Tile to create detector
        intra = np.tile(pixel_sens, (self.apsize, self.apsize))
        self.detector = np.zeros((res*self.apsize,res*self.apsize))

        # Multiply by inter-pixel sensitivity variables
        for i in range(self.apsize):
            for j in range(self.apsize):
                self.detector[i*res:(i+1)*res][j*res:(j+1)*res] = intra[i*res:(i+1)*res][j*res:(j+1)*res] * self.inter[i][j]

        # Display detector
        pl.imshow(self.detector, origin='lower', cmap='gray')

        return self.detector


    def FindCDPP(self, flux=[]):
        '''
        Quick function to calculate and return Combined Differential Photometric Precision (CDPP)
        '''

        # check if flux light curve was passed in
        if len(flux) == 0:
            flux = self.flux

        cdpp = CDPP(flux)

        return cdpp

    def Plot(self):
        '''
        Simple plotting function to view first cadence tpf, and both raw and de-trended flux light curves.
        '''

        # initialize subplots with 1:3 width ratio
        fig, ax = pl.subplots(1, 2, figsize=(12,3), gridspec_kw = {'width_ratios':[1, 3]})

        # Get aperture contour
        aperture = self.Aperture()


        def PadWithZeros(vector, pad_width, iaxis, kwargs):
            vector[:pad_width[0]] = 0
            vector[-pad_width[1]:] = 0
            return vector
        ny, nx = self.fpix[0].shape
        contour = np.zeros((ny, nx))
        contour[np.where(aperture==1)] = 1
        contour = np.lib.pad(contour, 1, PadWithZeros)
        highres = zoom(contour, 100, order=0, mode='nearest')
        extent = np.array([-1, nx, -1, ny])

        # display first cadence tpf
        ax[0].imshow(self.fpix[0], origin='lower', cmap='viridis', interpolation='nearest')
        ax[0].contour(highres, levels=[0.5], extent=extent, origin='lower', colors='r', linewidths=2)

        ax[0].set_title('First Cadence tpf')
        ax[0].set_xlabel('x (pixels)')
        ax[0].set_ylabel('y (pixels)')

        # plot raw and de-trend light curves
        det_flux = self.Detrend()[0]

        # make sure CDPP is a number before printing it
        if np.isnan(self.FindCDPP(self.flux)):
            ax[1].plot(self.t, self.flux, 'k.', label='raw flux')
            ax[1].plot(self.t, det_flux, 'r.', label='de-trended')
        else:
            ax[1].plot(self.t, self.flux, 'k.', label='raw flux (CDPP = %.i)' % self.FindCDPP(self.flux))
            ax[1].plot(self.t, det_flux, 'r.', label='de-trended (CDPP = %.i)' % self.FindCDPP(det_flux))
        ax[1].set_xlim([self.t[0], self.t[-1]])
        ax[1].legend(loc=0)
        ax[1].set_xlabel('Time (days)')
        ax[1].set_ylabel('Flux (counts)')
        ax[1].set_title('Flux Light Curve')

        fig.tight_layout()
        pl.show()
