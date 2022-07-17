#!/usr/bin/env python
# Same licence as
# https://github.com/smaeul/linux/blob/rk35/ebc-drm-v5/drivers/gpu/drm/ \
#   drm_epd_helper.c
import os
import ctypes
import struct

import numpy as np

from read_file import waveform_file


class epd_phase(object):
    """One phase of the lut"""
    def __init__(self, data=None):
        self.polarisations = np.zeros((32, 32))
        if data is not None:
            self.set(data)

    def set(self, data):
        if isinstance(data, list):
            assert len(data) == 1024
            self.polarisations = np.array(data).reshape((32, 32))
        else:
            assert data.shape == (32, 32)
            self.polarisations = data

    def to_bytes(self):
        """Return a list of bytes"""
        pols_flat = self.polarisations.flatten().astype(int)

        phase_packed = []
        for i in range(0, 1024, 4):
            phase_packed += [
                pols_flat[i] | pols_flat[i + 1] << 2 |
                pols_flat[i + 2] << 4 | pols_flat[i + 3] << 6
            ]
        return phase_packed


class epd_lut(object):
    """Multiple phases, forming one lut (max: 256 phases)"""
    token_end = int('ff', 16)

    def __init__(self, phases=None):
        if phases is not None:
            self.phases = phases
        else:
            self.phases = []

    def add(self, phase):
        self.phases += [phase]

    def to_bytes(self):
        all_phases = []
        index = 0
        for phase in self.phases:
            index += 1
            all_phases += phase.to_bytes()
        all_phases += [self.token_end]
        return all_phases

    def _encode_long_data(self, data_long):
        # re-encode a long byte stream

        token_repeat = int('fc', 16)
        token_end = int('ff', 16)
        data_rec = []
        index = 0

        def look_ahead(data, index_start):
            if index_start == len(data) - 1:
                return False, 0
            if data[index_start] == data[index_start + 1]:
                repeat = True
                index = index_start
                while(
                        index < len(data) - 1 and
                        data[index] == data[index + 1]):
                    index += 1
                return repeat, index - index_start
            else:
                repeat = False
                index = index_start
                while(
                        index < len(data) - 1 and
                        data[index] != data[index + 1]):
                    index += 1
                return repeat, index - index_start

        # tests
        # print('look 1', look_ahead([1, 1, 1, 2, 3, 4], 0))
        # print('look 2', look_ahead([1, 1, 2, 3, 4, 4], 0))
        # print('look 3', look_ahead([1, 1, 2, 3, 4, 4], 1))

        repeat_mode = True
        while(index < len(data_long[0:])):
            repeat, count = look_ahead(data_long, index)
            # print('look ahead', repeat, count)

            # we need to decide if its worthwhile to change the state
            if not repeat and repeat_mode:
                # print('Evaluating state')
                if count > 1:
                    # print('   deactivating at index', index, len(data_rec))
                    repeat_mode = False
                    data_rec += [token_repeat]

            if repeat and not repeat_mode:
                if count >= 2:
                    # print('   activating at index', index, len(data_rec))
                    repeat_mode = True
                    data_rec += [token_repeat]

            # do we have repeating entries
            if repeat:
                if repeat_mode:
                    # data_rec += [data_long[index], count]
                    counts = [255] * int(count / 256)
                    if count % 256 > 0:
                        counts += [count % 256]
                    for subcount in counts:
                        data_rec += [data_long[index], subcount]
                else:
                    for i in range(1 + count):
                        data_rec += [data_long[index]]

                index += count
            else:
                # no repetition ahead

                # do we need to switch?
                if repeat_mode:
                    data_rec += [data_long[index], 0]
                else:
                    data_rec += [data_long[index]]
            index += 1
            # break
            # if len(data_rec) > 115:
            #     break
        data_rec += [token_end]

        # check
        # for nr, (orig, rec) in enumerate(zip(data_orig, data_rec)):
        #     if orig != rec:
        #         print('Error at index: ', nr)

        # print(list(data_orig[len(data_rec) - 10: len(data_rec) + 10]))
        # print(data_rec[-10:])
        # print(data_long[index - 10: index + 10])
        return data_rec

    def to_bytes_compact(self):
        all_bytes = self.to_bytes()[:-1]
        return self._encode_long_data(all_bytes)


class epd_waveform(object):
    """Holds multiple luts for different temperatures"""
    def __init__(self):
        self.temp_luts = {}

    def add(self, temperature, waveform):
        self.temp_luts[temperature] = waveform

    def to_bytes(self):
        last_offset = 0
        offsets = {}
        data = []
        for temperature in sorted(self.temp_luts.keys()):
            subdata = self.temp_luts[temperature].to_bytes_compact()
            data += subdata
            offsets[temperature] = last_offset
            last_offset += len(subdata)
        return data, offsets


class epd_waveforms(object):
    def __init__(self):
        self.waveforms = []
        self.byte_data = None
        self.byte_offsets = None
        self.byte_wf_temp_offsets = None

    def add(self, waveform):
        self.waveforms.append(waveform)

    def _to_bytes(self):
        data = []
        wf_temp_offsets = []
        offset = 0
        offsets = []
        for waveform in self.waveforms:
            subdata, suboffsets = waveform.to_bytes()
            # we need to fix the suboffsets to account for shifts by previous
            # waveforms
            for key in suboffsets.keys():
                suboffsets[key] += offset
            wf_temp_offsets += [suboffsets]
            data += subdata
            offsets += [offset]
            offset = offset + len(subdata)
        return data, offsets, wf_temp_offsets

    def to_bytes(self, regenerate=False):
        if regenerate or not self.byte_data:
            data, offsets, wf_temp_offsets = self._to_bytes()
            self.byte_data = data
            self.byte_offsets = offsets
            self.byte_wf_temp_offsets = wf_temp_offsets

        return self.byte_data, self.byte_offsets, self.byte_wf_temp_offsets


class wf_file(object):
    def __init__(self):
        self.header = None
        self.waveforms = None
        # at this point not all bytes of the file format a clear. In case we
        # need byte information from an original file, add reference data here
        self.refdata = None

    def add_header(self, header):
        self.header = header

    def header_to_bytes(self):
        if self.header is None:
            return []
        he = self.header
        data = []
        data += struct.pack('<I', he['checksum'])
        data += struct.pack('<I', he['file_size'])
        data += struct.pack('<I', he['serial'])
        data += struct.pack('B', he['run_type'])
        data += struct.pack('B', he['fpl_platform'])
        data += struct.pack('H', he['fpl_lot'])
        data += struct.pack('B', he['mode_version'])
        data += struct.pack('B', he['wf_version'])
        data += struct.pack('B', he['wf_subversion'])
        data += struct.pack('B', he['wf_type'])
        data += struct.pack('B', he['panel_size'])
        data += struct.pack('B', he['amepd_part_number'])
        data += struct.pack('B', he['wf_rev'])
        data += struct.pack('B', he['frame_rate_bcd'])
        data += struct.pack('B', he['frame_rate_hex'])
        data += struct.pack('B', he['vcom_offset'])
        data += struct.pack('B', he['unknown_1'])
        data += struct.pack('B', he['unknown_2'])
        data += struct.pack('B', he['xwia_1'])
        data += struct.pack('B', he['xwia_2'])
        data += struct.pack('B', he['xwia_3'])
        data += struct.pack('B', he['cs1'])
        data += struct.pack('B', he['wmta_1'])
        data += struct.pack('B', he['wmta_2'])
        data += struct.pack('B', he['wmta_3'])
        data += struct.pack('B', he['fvsn'])
        data += struct.pack('B', he['luts'])
        data += struct.pack('B', he['mode_count'] - 1)
        data += struct.pack('B', he['temp_range_count'])
        data += struct.pack('B', he['advanced_wf_flags'])
        data += struct.pack('B', he['eb'])
        data += struct.pack('B', he['sb'])
        data += struct.pack('B', he['reserved_1'])
        data += struct.pack('B', he['reserved_2'])
        data += struct.pack('B', he['reserved_3'])
        data += struct.pack('B', he['reserved_4'])
        data += struct.pack('B', he['reserved_5'])
        data += struct.pack('B', he['cs2'])

        for temperature in he['temperatures']:
            data += struct.pack('B', temperature)

        return data

    def add_waveforms(self, waveforms):
        self.waveforms = waveforms

    def set_reference_data(self, refdata):
        self.refdata = refdata

    def to_bytes(self):
        """

        Parameters
        ----------
        ref_data : list
            Reference data that we use to fill data bytes with unknown origin
            or that we are too lazy to properly generate/import



        Major blocks:
        [HEADER]
        [MODE_TABLE]
        [TEMP_TABLES]
        [DATA]
        """
        header = self.header

        data = []
        # add header here, assuming we do not change the mode_table offset or
        # temperature values
        data += self.header_to_bytes()

        lut_data, lut_offsets, wf_temp_offsets = all_waveforms.to_bytes()

        # mode table
        mode_table_offset = header[
            'wmta_1'
        ] | header[
            'wmta_2'
        ] << 8 | header[
            'wmta_3'] << 16

        # use the reference data to fill bytes between end of header and
        # beginning of mode table (mostly this is the filename)
        if self.refdata is None:
            # fill with zeros
            data += [0] * (mode_table_offset - len(data))
        else:
            data += self.refdata[len(data): mode_table_offset]

        # mode table starts by default at 106
        # we go the easy way and define data to start at byte 1050 (as in the
        # original waveform file)
        data_offset = 1050

        # let the temp table begin at
        temp_table_offset_0 = 138
        # for some reason each temp table has an offset to the next of 112
        # bytes, although the offset itself is only 4 bytes long
        tt_increment = 112

        mode_table = list(range(
            temp_table_offset_0,
            # sry, I'm lazy, 8 modes
            temp_table_offset_0 + 8 * tt_increment,
            tt_increment
        ))

        for of in mode_table:
            b1 = of & 255
            b2 = of >> 8 & 255
            b3 = of >> 16 & 255
            b4 = b1 + b2 + b3
            data += [b1, b2, b3, b4]

        assert len(data) == temp_table_offset_0
        # create the temperature tables
        # temp_tables = {}
        # for index, offset in enumerate(mode_table):

        for base, table in zip(mode_table, wf_temp_offsets):
            # add zeros until we arrive at the base address
            while len(data) < base:
                data += [0]
            for temp, of in sorted(table.items()):
                off = of + data_offset
                b1 = off & 255
                b2 = off >> 8 & 255
                b3 = off >> 16 & 255
                b4 = ctypes.c_ubyte(b1 + b2 + b3).value
                data += [b1, b2, b3, b4]

        while len(data) < data_offset:
            data += [0]

        for nr, x in enumerate(data):
            if x > 255:
                print(
                    'Error: Byte '
                    '{} has a supposed value larger than 255'.format(nr)
                )
                break

        data += lut_data
        # print('Ipy')
        # import IPython
        # IPython.embed()

        # fix file size
        header['file_size'] = len(data)
        new_header = self.header_to_bytes()
        data[0:len(new_header)] = new_header
        # IPython.embed()
        return data

    def export_to_file(self, filename, overwrite=False):
        if not overwrite and os.path.isfile(filename):
            raise Exception(
                'Output filename {} already exists'.format(filename)
            )
        result = nwff.to_bytes()

        with open(filename, 'wb') as fid:
            fid.write(bytes(result))


if __name__ == '__main__':
    wff = waveform_file('02_waveform.img')

    # import the existing file
    nwff = wf_file()
    nwff.add_header(wff.header())
    all_waveforms = epd_waveforms()
    for lutnr in range(wff.header()['mode_count']):
        waveform = epd_waveform()
        for temp in wff.header()['temperatures']:
            lut_in, lut_orig = wff.get_lut(lutnr, temp, return_compressed=True)
            phases = []
            for i in np.arange(0, len(lut_in), 1024):
                pha = epd_phase(lut_in[i:i+1024])
                phases.append(pha)
            lut = epd_lut(phases)
            waveform.add(temp, lut)
            rec = lut.to_bytes_compact()
            # check the original and recovered lut
            # for index, (a, b) in enumerate(zip(lut_orig, rec)):
            #     if a != b:
            #         print('error', index)
            #         raise Exception('orig and rec not the same')

            # break
        all_waveforms.add(waveform)
        # if lutnr == 1:
        #     break
    nwff.add_waveforms(all_waveforms)

    # later: modification
    for i in range(4):
        del(nwff.waveforms.waveforms[6].temp_luts[18].phases[0])
        del(nwff.waveforms.waveforms[6].temp_luts[21].phases[0])
        del(nwff.waveforms.waveforms[6].temp_luts[24].phases[0])
        del(nwff.waveforms.waveforms[6].temp_luts[27].phases[0])

    # Warning: This probably DC-biases the panel!!!
    # for temp in [18, 21, 24, 27]:
    #     for phase in nwff.waveforms.waveforms[6].temp_luts[temp].phases[:-1]:
    #         # white -> white
    #         # make whiter. Keep in mind that this will not work in
    #         # diff_mode!!!
    #         phase.polarisations[30, 30] = 2
    #         phase.polarisations[30, 31] = 2
    #         phase.polarisations[30, 29] = 2

    #         # # black -> black
    #         phase.polarisations[0, 0] = 1
    #         phase.polarisations[0, 1] = 1
    #         phase.polarisations[0, 3] = 1

    # export
    nwff.export_to_file('test.bin')
