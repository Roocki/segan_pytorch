import torch
import soundfile as sf
import numpy as np
import struct
import librosa
import json
import pickle
import random
import pysptk
from ahoproc_tools.interpolate import *
import torch.nn.functional as F
from scipy import interpolate
from scipy import signal
from scipy.signal import decimate
from scipy.io import loadmat
from scipy.signal import lfilter
from scipy.interpolate import interp1d
import glob
import os


class PCompose(object):

    def __init__(self, transforms, probs=0.4, report=False):
        assert isinstance(transforms, list), type(transforms)
        self.transforms = transforms
        self.probs = probs
        self.report = report

    def __call__(self, tensor):
        x = tensor
        reports = []
        for transf in self.transforms:
            if random.random() <= self.probs:
                x = transf(x)
                if len(x) == 2:
                    # get the report
                    x, report = x
                    reports.append(report)
        if self.report:
            return x, reports
        else:
            return x

class SwitchCompose(object):

    def __init__(self, transforms, report=False):
        assert isinstance(transforms, list), type(transforms)
        self.transforms = transforms
        self.report = report

    def __call__(self, tensor):
        x = tensor
        reports = []
        idxs = list(range(len(self.transforms)))
        idx = random.choice(idxs)
        transf = self.transforms[idx]
        x = transf(x)
        if len(x) == 2:
            # get the report
            x, report = x
            reports.append(report)
        if self.report:
            return x, reports
        else:
            return x

class Scale(object):
    """Scale audio tensor from a 16-bit integer (represented as a FloatTensor)
    to a floating point number between -1.0 and 1.0.  Note the 16-bit number is
    called the "bit depth" or "precision", not to be confused with "bit rate".
    Args:
        factor (int): maximum value of input tensor. default: 16-bit depth
    """

    def __init__(self, factor=2**31):
        self.factor = factor

    def __call__(self, tensor):
        """
        Args:
            tensor (Tensor): Tensor of audio of size (Samples x Channels)
        Returns:
            Tensor: Scaled by the scale factor. (default between -1.0 and 1.0)
        """
        if isinstance(tensor, (torch.LongTensor, torch.IntTensor)):
            tensor = tensor.float()

        return tensor / self.factor

class ToTensor(object):

    def __call__(self, *raws):
        if isinstance(raws, list):
            # Convert all tensors passed in list
            res = []
            for raw in raws:
                res.append(self(raw))
            return res
        if not isinstance(raws, torch.Tensor):
            raws = torch.tensor(raws)
        return raws

    def __repr__(self):
        return self.__class__.__name__ + '()'

class SingleChunkWav(object):

    def __init__(self, chunk_size, report=False):
        self.chunk_size = chunk_size
        self.report = report

    def assert_format(self, x):
        # assert it is a waveform and pytorch tensor
        assert isinstance(x, torch.Tensor), type(x)
        assert x.dim() == 1, x.size()

    def __call__(self, *raw):
        # can be many raw signals at a time,
        # all of them chunked at same place
        # select random index
        chksz = self.chunk_size
        idx = None
        rets = []
        for w_ in raw:
            if idx is None:
                idxs = list(range(w_.size(0) - chksz))
                if len(idxs) == 0:
                    idxs = [0]
                idx = random.choice(idxs)
            if w_.size(0) < chksz:
                P = chksz - w_.size(0)
                w_ = torch.cat((w_.float(),
                                torch.zeros(P)), dim=0)
            chk = w_[idx:idx + chksz]
            rets.append(chk)
        if len(rets) == 1 and not self.report:
            return rets[0]
        elif len(rets) > 1 and not self.report:
            return rets
        else:
            rets += [{'beg_i':idx, 'end_i':idx + chksz}]
            return rets

    def __repr__(self):
        return self.__class__.__name__ + \
                '({})'.format(self.chunk_size)

class Reverb(object):

    def __init__(self, ir_file):
        ir_ext = os.path.splitext(ir_file)[1]
        assert ir_ext == '.mat', ir_ext
        self.IR, self.p_max = self.load_IR(ir_file)

    def load_IR(self, ir_file):
        IR = loadmat(ir_file, squeeze_me=True, struct_as_record=False)
        IR = IR['risp_imp']
        IR = IR / np.abs(np.max(IR))
        p_max = np.argmax(np.abs(IR))
        return IR, p_max

    def shift(self, xs, n):
        e = np.empty_like(xs)
        if n >= 0:
            e[:n] = 0.0
            e[n:] = xs[:-n]
        else:
            e[n:] = 0.0
            e[:n] = xs[-n:]
        return e

    def __call__(self, wav):
        if torch.is_tensor(wav):
            wav = wav.data.numpy()
        wav = wav.astype(np.float64)
        wav = wav / np.max(np.abs(wav))
        rev = signal.fftconvolve(wav, self.IR, mode='full')
        rev = rev / np.max(np.abs(rev))
        # IR delay compensation
        rev = self.shift(rev, -self.p_max)
        # Trim rev signal to match clean length
        rev = rev[:wav.shape[0]]
        return torch.FloatTensor(rev)

class Additive(object):

    def __init__(self, noises_dir, snr_levels=[0, 5, 10], do_IRS=False,
                 prob=1):
        self.prob = prob
        self.noises_dir = noises_dir
        self.snr_levels = snr_levels
        self.do_IRS = do_IRS
        # read noises in dir
        noises = glob.glob(os.path.join(noises_dir, '*.wav'))
        if len(noises) == 0:
            raise ValueError('[!] No noises found in {}'.format(noises_dir))
        else:
            print('[*] Found {} noise files'.format(len(noises)))
            self.noises = []
            for n_i, npath in enumerate(noises, start=1):
                #nwav = wavfile.read(npath)[1]
                nwav = librosa.load(npath, sr=None)[0]
                self.noises.append({'file':npath, 
                                    'data':nwav.astype(np.float32)})
                log_noise_load = 'Loaded noise {:3d}/{:3d}: ' \
                                 '{}'.format(n_i, len(noises),
                                             npath)
                print(log_noise_load)
        self.eps = 1e-22

    def __call__(self, wav, srate=16000, nbits=16):
        """ Add noise to clean wav """
        if isinstance(wav, torch.Tensor):
            wav = wav.numpy()
        noise_idx = np.random.choice(list(range(len(self.noises))), 1)
        sel_noise = self.noises[np.asscalar(noise_idx)]
        noise = sel_noise['data']
        snr = np.random.choice(self.snr_levels, 1)
        # print('Applying SNR: {} dB'.format(snr[0]))
        if wav.ndim > 1:
            wav = wav.reshape((-1,))
        noisy, noise_bound = self.addnoise_asl(wav, noise, srate, 
                                               nbits, snr, 
                                               do_IRS=self.do_IRS)
        # normalize to avoid clipping
        if np.max(noisy) >= 1 or np.min(noisy) < -1:
            small = 0.1
            while np.max(noisy) >= 1 or np.min(noisy) < -1:
                noisy = noisy / (1. + small)
                small = small + 0.1
        return torch.FloatTensor(noisy.astype(np.float32))


    def addnoise_asl(self, clean, noise, srate, nbits, snr, do_IRS=False):
        if do_IRS:
            # Apply IRS filter simulating telephone 
            # handset BW [300, 3200] Hz
            clean = self.apply_IRS(clean, srate, nbits)
        Px, asl, c0 = self.asl_P56(clean, srate, nbits)
        # Px is active speech level ms energy
        # asl is active factor
        # c0 is active speech level threshold
        x = clean
        x_len = x.shape[0]

        noise_len = noise.shape[0]
        if noise_len <= x_len:
            print('Noise length: ', noise_len)
            print('Speech length: ', x_len)
            raise ValueError('Noise length has to be greater than speech '
                             'length!')
        rand_start_limit = int(noise_len - x_len + 1)
        rand_start = int(np.round((rand_start_limit - 1) * np.random.rand(1) \
                                  + 1))
        noise_segment = noise[rand_start:rand_start + x_len]
        noise_bounds = (rand_start, rand_start + x_len)

        if do_IRS:
            noise_segment = self.apply_IRS(noise_segment, srate, nbits)

        Pn = np.dot(noise_segment.T, noise_segment) / x_len

        # we need to scale the noise segment samples to obtain the 
        # desired SNR = 10 * log10( Px / ((sf ** 2) * Pn))
        sf = np.sqrt(Px / Pn / (10 ** (snr / 10)))
        noise_segment = noise_segment * sf
    
        noisy = x + noise_segment

        return noisy, noise_bounds

    def apply_IRS(self, data, srate, nbits):
        """ Apply telephone handset BW [300, 3200] Hz """
        raise NotImplementedError('Under construction!')
        from pyfftw.interfaces import scipy_fftpack as fftw
        n = data.shape[0]
        # find next pow of 2 which is greater or eq to n
        pow_of_2 = 2 ** (np.ceil(np.log2(n)))

        align_filter_dB = np.array([[0, -200], [50, -40], [100, -20],
                           [125, -12], [160, -6], [200, 0],
                           [250, 4], [300, 6], [350, 8], [400, 10],
                           [500, 11], [600, 12], [700, 12], [800, 12],
                           [1000, 12], [1300, 12], [1600, 12], [2000, 12],
                           [2500, 12], [3000, 12], [3250, 12], [3500, 4],
                           [4000, -200], [5000, -200], [6300, -200], 
                           [8000, -200]]) 
        print('align filter dB shape: ', align_filter_dB.shape)
        num_of_points, trivial = align_filter_dB.shape
        overallGainFilter = interp1d(align_filter_dB[:, 0], align_filter[:, 1],
                                     1000)

        x = np.zeros((pow_of_2))
        x[:data.shape[0]] = data

        x_fft = fftw.fft(x, pow_of_2)

        freq_resolution = srate / pow_of_2

        factorDb = interp1d(align_filter_dB[:, 0],
                            align_filter_dB[:, 1],
                                           list(range(0, (pow_of_2 / 2) + 1) *\
                                                freq_resolution)) - \
                                           overallGainFilter
        factor = 10 ** (factorDb / 20)

        factor = [factor, np.fliplr(factor[1:(pow_of_2 / 2 + 1)])]
        x_fft = x_fft * factor

        y = fftw.ifft(x_fft, pow_of_2)

        data_filtered = y[:n]
        return data_filtered


    def asl_P56(self, x, srate, nbits):
        """ ITU P.56 method B. """
        T = 0.03 # time constant of smoothing in seconds
        H = 0.2 # hangover time in seconds
        M = 15.9

        # margin in dB of the diff b/w threshold and active speech level
        thres_no = nbits - 1 # num of thresholds, for 16 bits it's 15

        I = np.ceil(srate * H) # hangover in samples
        g = np.exp( -1 / (srate * T)) # smoothing factor in envelop detection
        c = 2. ** (np.array(list(range(-15, (thres_no + 1) - 16))))
        # array of thresholds from one quantizing level up to half the max
        # code, at a step of 2. In case of 16bit: from 2^-15 to 0.5
        a = np.zeros(c.shape[0]) # activity counter for each level thres
        hang = np.ones(c.shape[0]) * I # hangover counter for each level thres

        assert x.ndim == 1, x.shape
        sq = np.dot(x, x) # long term level square energy of x
        x_len = x.shape[0]

        # use 2nd order IIR filter to detect envelope q
        x_abs = np.abs(x)
        p = lfilter(np.ones(1) - g, np.array([1, -g]), x_abs)
        q = lfilter(np.ones(1) - g, np.array([1, -g]), p)

        for k in range(x_len):
            for j in range(thres_no):
                if q[k] >= c[j]:
                    a[j] = a[j] + 1
                    hang[j] = 0
                elif hang[j] < I:
                    a[j] = a[j] + 1
                    hang[j] = hang[j] + 1
                else:
                    break
        asl = 0
        asl_ms = 0
        c0 = None
        if a[0] == 0:
            return asl_ms, asl, c0
        else:
            den = a[0] + self.eps
            AdB1 = 10 * np.log10(sq / a[0] + self.eps)
        
        CdB1 = 20 * np.log10(c[0] + self.eps)
        if AdB1 - CdB1 < M:
            return asl_ms, asl, c0
        AdB = np.zeros(c.shape[0])
        CdB = np.zeros(c.shape[0])
        Delta = np.zeros(c.shape[0])
        AdB[0] = AdB1
        CdB[0] = CdB1
        Delta[0] = AdB1 - CdB1

        for j in range(1, AdB.shape[0]):
            AdB[j] = 10 * np.log10(sq / (a[j] + self.eps) + self.eps)
            CdB[j] = 20 * np.log10(c[j] + self.eps)

        for j in range(1, Delta.shape[0]):
            if a[j] != 0:
                Delta[j] = AdB[j] - CdB[j]
                if Delta[j] <= M:
                    # interpolate to find the asl
                    asl_ms_log, cl0 = self.bin_interp(AdB[j],
                                                      AdB[j - 1],
                                                      CdB[j],
                                                      CdB[j - 1],
                                                      M, 0.5)
                    asl_ms = 10 ** (asl_ms_log / 10)
                    asl = (sq / x_len ) / asl_ms
                    c0 = 10 ** (cl0 / 20)
                    break
        return asl_ms, asl, c0

    def bin_interp(self, upcount, lwcount, upthr, lwthr, Margin, tol):
        if tol < 0:
            tol = -tol

        # check if extreme counts are not already the true active value
        iterno = 1
        if np.abs(upcount - upthr - Margin) < tol:
            asl_ms_log = lwcount
            cc = lwthr
            return asl_ms_log, cc
        if np.abs(lwcount - lwthr - Margin) < tol:
            asl_ms_log = lwcount
            cc =lwthr
            return asl_ms_log, cc

        midcount = (upcount + lwcount) / 2
        midthr = (upthr + lwthr) / 2
        # repeats loop until diff falls inside tolerance (-tol <= diff <= tol)
        while True:
            diff = midcount - midthr - Margin
            if np.abs(diff) <= tol:
                break
            # if tol is not met up to 20 iters, then relax tol by 10%
            iterno += 1
            if iterno > 20:
                tol *= 1.1

            if diff > tol:
                midcount = (upcount + midcount) / 2
                # upper and mid activities
                midthr = (upthr + midthr) / 2
                # ... and thresholds
            elif diff < -tol:
                # then new bounds are...
                midcount = (midcount - lwcount) / 2
                # middle and lower activities
                midthr = (midthr + lwthr) / 2
                # ... and thresholds
        # since tolerance has been satisfied, midcount is selected as
        # interpolated value with tol [dB] tolerance
        asl_ms_log = midcount
        cc = midthr
        return asl_ms_log, cc

    def __repr__(self):
        attrs = '(noises_dir={}\n, snr_levels={}\n, do_IRS={})'.format(
            self.noises_dir,
            self.snr_levels,
            self.do_IRS
        )
        return self.__class__.__name__ + attrs


class Chopper(object):
    def __init__(self, chop_factors=[(0.05, 0.025), (0.1, 0.05)],
                 max_chops=2, report=False):
        # chop factors in seconds (mean, std) per possible chop
        import webrtcvad
        self.chop_factors = chop_factors
        self.max_chops = max_chops
        # create VAD to get speech chunks
        self.vad = webrtcvad.Vad(2)
        # make scalers to norm/denorm
        self.denormalizer = Scale(1. / ((2 ** 15) - 1))
        self.normalizer = Scale((2 ** 15) - 1)
        self.report = report

    def vad_wav(self, wav, srate):
        """ Detect the voice activity in the 16-bit mono PCM wav and return
            a list of tuples: (speech_region_i_beg_sample, center_sample, 
            region_duration)
        """
        if srate != 16000:
            raise ValueError('Sample rate must be 16kHz')
        window_size = 160 # samples
        regions = []
        curr_region_counter = 0
        init = None
        vad = self.vad
        # first run the vad across the full waveform
        for beg_i in range(0, wav.shape[0], window_size):
            frame = wav[beg_i:beg_i + window_size]
            if frame.shape[0] >= window_size and \
               vad.is_speech(struct.pack('{}i'.format(window_size), 
                                         *frame), srate):
                curr_region_counter += 1
                if init is None:
                    init = beg_i
            else:
                # end of speech region (or never began yet)
                if init is not None:
                    # close the region
                    end_sample = init + (curr_region_counter * window_size)
                    center_sample = init + (end_sample - init) / 2
                    regions.append((init, center_sample, 
                                    curr_region_counter * window_size))
                init = None
                curr_region_counter = 0
        return regions

    def chop_wav(self, wav, srate, speech_regions):
        if len(speech_regions) == 0:
            #print('Skipping no speech regions')
            return wav
        chop_factors = self.chop_factors
        # get num of chops to make
        num_chops = list(range(1, self.max_chops + 1))
        chops = np.asscalar(np.random.choice(num_chops, 1))
        # trim it to available regions
        chops = min(chops, len(speech_regions))
        # build random indexes to randomly pick regions, not ordered
        if chops == 1:
            chop_idxs = [0]
        else:
            chop_idxs = np.random.choice(list(range(chops)), chops, 
                                         replace=False)
        chopped_wav = np.copy(wav)
        # make a chop per chosen region
        for chop_i in chop_idxs:
            region = speech_regions[chop_i]
            # decompose the region
            reg_beg, reg_center, reg_dur = region
            # pick random chop_factor
            chop_factor_idx = np.random.choice(range(len(chop_factors)), 1)[0]
            chop_factor = chop_factors[chop_factor_idx]
            # compute duration from: std * N(0, 1) + mean
            mean, std = chop_factor
            chop_dur = mean + np.random.randn(1) * std
            # convert dur to samples
            chop_s_dur = int(chop_dur * srate)
            chop_beg = max(int(reg_center - (chop_s_dur / 2)), reg_beg)
            chop_end = min(int(reg_center + (chop_s_dur / 2)), reg_beg +
                           reg_dur)
            #print('chop_beg: ', chop_beg)
            #print('chop_end: ', chop_end)
            # chop the selected region with computed dur
            chopped_wav[chop_beg:chop_end] = 0
        return chopped_wav

    def __call__(self, wav, srate=16000):
        if isinstance(wav, np.ndarray):
            wav = torch.FloatTensor(wav)
        # unorm to 16-bit scale for VAD in chopper
        wav = self.denormalizer(wav)
        if isinstance(wav, torch.Tensor):
            wav = wav.numpy()
        wav = wav.astype(np.int16)
        if wav.ndim > 1:
            wav = wav.reshape((-1,))
        # get speech regions for proper chopping
        speech_regions = self.vad_wav(wav, srate)
        chopped = self.chop_wav(wav, srate, 
                                speech_regions).astype(np.float32)
        chopped = self.normalizer(torch.FloatTensor(chopped))
        if self.report:
            report = {'speech_regions':speech_regions}
            return chopped, report
        return chopped

    def __repr__(self):
        attrs = '(chop_factors={}, max_chops={})'.format(
            self.chop_factors,
            self.max_chops
        )
        return self.__class__.__name__ + attrs

class Clipping(object):

    def __init__(self, clip_factors = [0.3, 0.4, 0.5],
                 report=False):
        self.clip_factors = clip_factors
        self.report = report

    def __call__(self, wav):
        if isinstance(wav, torch.Tensor):
            wav = wav.numpy()
        cf = np.random.choice(self.clip_factors, 1)
        clip = np.maximum(wav, cf * np.min(wav))
        clip = np.minimum(clip, cf * np.max(wav))
        clipT = torch.FloatTensor(clip)
        if self.report:
            report = {'clip_factor':np.asscalar(cf)}
            return clipT, report
        return clipT

    def __repr__(self):
        attrs = '(clip_factors={})'.format(
            self.clip_factors
        )
        return self.__class__.__name__ + attrs

class Resample(object):

    def __init__(self, factors=[4], report=False):
        self.factors = factors
        self.report = report

    def __call__(self, wav):
        if isinstance(wav, torch.Tensor):
            wav = wav.numpy()
        factor = random.choice(self.factors)
        x_lr = decimate(wav, factor).copy()
        x_lr = torch.FloatTensor(x_lr)
        x_ = F.interpolate(x_lr.view(1, 1, -1), 
                           scale_factor=factor,
                           align_corners=True,
                           mode='linear').view(-1)
        if self.report:
            report = {'resample_factor':factor}
            return x_, report
        return x_

    def __repr__(self):
        attrs = '(factor={})'.format(
            self.factors
        )
        return self.__class__.__name__ + attrs

class AcoFeats(object):

    def __init__(self, hop=256, win=512, n_fft=2048, min_f0=60, max_f0=300,
                 sr=16000, order=16):
        self.hop = hop
        self.win = win
        self.n_fft = n_fft
        self.min_f0 = min_f0
        self.max_f0 = max_f0
        self.sr = sr
        self.order = order

    def __call__(self, wav):
        if isinstance(wav, torch.Tensor):
            wav_npy = wav.numpy()
        else:
            wav_npy = wav
            wav = torch.tensor(wav)
        lps = self.extract_lps(wav)
        mfcc = self.extract_mfcc(wav_npy)
        proso = self.extract_prosody(wav_npy)
        aco = torch.cat((lps, mfcc, proso), dim=0)
        return aco

    def extract_prosody(self, wav):
        max_frames = wav.shape[0] // self.hop
        # first compute logF0 and voiced/unvoiced flag
        f0 = pysptk.swipe(wav.astype(np.float64),
                          fs=self.sr, hopsize=self.hop,
                          min=self.min_f0,
                          max=self.max_f0,
                          otype='f0')
        lf0 = np.log(f0 + 1e-10)
        lf0, uv = interpolation(lf0, -1)
        lf0 = torch.tensor(lf0.astype(np.float32)).unsqueeze(0)[:, :max_frames]
        uv = torch.tensor(uv.astype(np.float32)).unsqueeze(0)[:, :max_frames]
        if torch.sum(uv) == 0:
            # if frame is completely unvoiced, make lf0 min val
            lf0 = torch.ones(uv.size()) * np.log(self.min_f0)
        assert lf0.min() > 0, lf0.data.numpy()
        # secondly obtain zcr
        zcr = librosa.feature.zero_crossing_rate(y=wav,
                                                 frame_length=self.win,
                                                 hop_length=self.hop)
        zcr = torch.tensor(zcr.astype(np.float32))
        zcr = zcr[:, :max_frames]
        # finally obtain energy
        egy = librosa.feature.rmse(y=wav, frame_length=self.win,
                                   hop_length=self.hop,
                                   pad_mode='constant')
        egy = torch.tensor(egy.astype(np.float32))
        egy = egy[:, :max_frames]
        proso = torch.cat((lf0, uv, egy, zcr), dim=0)
        return proso


    def extract_mfcc(self, wav):
        max_frames = wav.shape[0] // self.hop
        mfcc = librosa.feature.mfcc(wav, sr=self.sr,
                                    n_mfcc=self.order,
                                    n_fft=self.n_fft,
                                    hop_length=self.hop
                                   )[:, :max_frames]
        return torch.tensor(mfcc.astype(np.float32))

    def extract_lps(self, wav):
        X = torch.stft(wav, self.n_fft,
                       self.hop, self.win)
        max_frames = wav.size(0) // self.hop
        X = torch.norm(X, 2, dim=2)[:, :max_frames]
        lps = 10 * torch.log10(X ** 2 + 10e-20)
        return lps



    def __repr__(self):
        attrs = '(hop={}, win={}, n_fft={})'.format(
            self.hop, self.win, self.n_fft
        )
        return self.__class__.__name__ + attrs

class ZNorm(object):

    def __init__(self, stats):
        self.stats_name = stats
        with open(stats, 'rb') as stats_f:
            self.stats = pickle.load(stats_f)

    def __call__(self, x):
        assert isinstance(x, torch.Tensor), type(x)
        mean = torch.tensor(self.stats['mean']).view(-1, 1).float()
        std = torch.tensor(self.stats['std']).view(-1, 1).float()
        y = (x - mean)/ std
        return y

    def __repr__(self):
        return self.__class__.__name__ + '({})'.format(self.stats_name)

if __name__ == '__main__':
    """
    wav, rate = librosa.load('test.wav', sr=None)
    wav = torch.FloatTensor(wav)
    chopper = Chopper(max_chops=10)
    chopped = chopper(wav, 16000)
    sf.write('chopped_test.wav', chopped.data.numpy(), 16000)

    clipper = Clipping(clip_factors=[0.1, 0.2, 0.3])
    clipped = clipper(chopped)
    sf.write('clipped_test.wav', clipped.data.numpy(), 16000)
    """
    import numpy as np
    from torchvision.transforms import Compose
    n2t = ToTensor()
    chk = SingleChunkWav(16000, report=True)
    x1 = np.zeros((20000))
    x2 = np.zeros((20000))
    X1, X2 = n2t(x1, x2)
    print('X1 size: ', X1.size())
    print('X2 size: ', X2.size())
    C1, C2, report = chk(X1, X2)
    print('C1 size: ', C1.size())
    print('C2 size: ', C2.size())
    print(report)
    fft = FFT(160, 400, 2048)
    x = torch.randn(16000)
    y = fft(x)
    print('y = fft(x) size: ', y.size())
