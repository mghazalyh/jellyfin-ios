/**
 * Actual default PS3 3.6.5 PlaybackInfo identity and strict 720p profile.
 * source/api/api_detail.cpp body_sd in mghazalyh/JellyFin-PS3.
 *
 * Android/iOS-native decode differs from PS3 VDEC/RSX/CellAudio. The
 * strict profile is opt-in and leaves regular iOS playback unchanged.
 */
import { CodecType } from '@jellyfin/sdk/lib/generated-client/models/codec-type';
import type { DeviceProfile } from '@jellyfin/sdk/lib/generated-client/models/device-profile';
import { DlnaProfileType } from '@jellyfin/sdk/lib/generated-client/models/dlna-profile-type';
import { EncodingContext } from '@jellyfin/sdk/lib/generated-client/models/encoding-context';
import { MediaStreamProtocol } from '@jellyfin/sdk/lib/generated-client/models/media-stream-protocol';
import { ProfileConditionType } from '@jellyfin/sdk/lib/generated-client/models/profile-condition-type';
import { ProfileConditionValue } from '@jellyfin/sdk/lib/generated-client/models/profile-condition-value';
import { SubtitleDeliveryMethod } from '@jellyfin/sdk/lib/generated-client/models/subtitle-delivery-method';

const Ps3SurfProfile = {
	Name: 'PS3',
	MaxStreamingBitrate: 8000000,
	MaxStaticBitrate: 8000000,
	MusicStreamingTranscodingBitrate: 192000,
	DirectPlayProfiles: [],
	TranscodingProfiles: [{
		Type: DlnaProfileType.Video,
		Container: 'ts',
		VideoCodec: 'h264',
		AudioCodec: 'mp3',
		Protocol: MediaStreamProtocol.Http,
		Context: EncodingContext.Streaming,
		MaxAudioChannels: '2'
	}],
	CodecProfiles: [{
		Type: CodecType.Video,
		Codec: 'h264',
		Conditions: [
			{
				Condition: ProfileConditionType.EqualsAny,
				Property: ProfileConditionValue.VideoProfile,
				Value: 'baseline',
				IsRequired: true
			},
			{
				Condition: ProfileConditionType.LessThanEqual,
				Property: ProfileConditionValue.VideoLevel,
				Value: '31',
				IsRequired: true
			},
			{
				Condition: ProfileConditionType.LessThanEqual,
				Property: ProfileConditionValue.Width,
				Value: '1280',
				IsRequired: true
			},
			{
				Condition: ProfileConditionType.LessThanEqual,
				Property: ProfileConditionValue.Height,
				Value: '720',
				IsRequired: true
			},
			{
				Condition: ProfileConditionType.LessThanEqual,
				Property: ProfileConditionValue.VideoBitrate,
				Value: '4000000',
				IsRequired: true
			}
		]
	}, {
		Type: CodecType.VideoAudio,
		Codec: 'mp3',
		Conditions: [
			{
				Condition: ProfileConditionType.LessThanEqual,
				Property: ProfileConditionValue.AudioChannels,
				Value: '2',
				IsRequired: false
			},
			{
				Condition: ProfileConditionType.Equals,
				Property: ProfileConditionValue.AudioSampleRate,
				Value: '48000',
				IsRequired: false
			}
		]
	}],
	ContainerProfiles: [],
	SubtitleProfiles: [
		...([ 'subrip', 'srt', 'ass', 'ssa', 'vtt' ].map(format => ({
			Format: format, Method: SubtitleDeliveryMethod.External
		}))),
		{ Format: 'pgssub', Method: SubtitleDeliveryMethod.Encode },
		{ Format: 'dvdsub', Method: SubtitleDeliveryMethod.Encode }
	]
} satisfies DeviceProfile;

export default Ps3SurfProfile;
