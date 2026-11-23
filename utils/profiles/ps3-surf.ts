/**
 * PS3 Surf transport diagnostic profile for the native iOS player.
 *
 * The PS3 3.6.5 client consumes a continuous MPEG-TS transport with H.264
 * video, timestamps and audio fed through its own decoder/jitter buffer.
 * iPad AVPlayer is not the PS3 RSX/CellAudio implementation. This is a
 * deliberate, reversible negotiation experiment, not a hardware emulator.
 */
import type { DeviceProfile } from '@jellyfin/sdk/lib/generated-client/models/device-profile';
import { DlnaProfileType } from '@jellyfin/sdk/lib/generated-client/models/dlna-profile-type';
import { EncodingContext } from '@jellyfin/sdk/lib/generated-client/models/encoding-context';
import { MediaStreamProtocol } from '@jellyfin/sdk/lib/generated-client/models/media-stream-protocol';

import iOSProfile from './ios-13';

const Ps3SurfProfile = {
	...iOSProfile,
	Name: 'PS3 Surf HTTP MPEG-TS Diagnostic (iPad)',
	DirectPlayProfiles: [
		{
			AudioCodec: 'aac,mp3,ac3',
			Container: 'ts,mpegts',
			Type: DlnaProfileType.Video,
			VideoCodec: 'h264'
		},
		...iOSProfile.DirectPlayProfiles
	],
	TranscodingProfiles: [
		...iOSProfile.TranscodingProfiles.filter(profile => profile.Type === DlnaProfileType.Audio),
		{
			AudioCodec: 'aac',
			Container: 'ts',
			Context: EncodingContext.Streaming,
			MaxAudioChannels: '2',
			Protocol: MediaStreamProtocol.Http,
			Type: DlnaProfileType.Video,
			VideoCodec: 'h264'
		}
	]
} satisfies DeviceProfile;

export default Ps3SurfProfile;
