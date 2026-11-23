/**
 * Copyright (c) 2025 Jellyfin Contributors
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/.
 */

import { MediaType } from '@jellyfin/sdk/lib/generated-client/models/media-type';
import { Audio, InterruptionModeAndroid, InterruptionModeIOS, Video, VideoFullscreenUpdate } from 'expo-av';
import React, { useEffect, useRef, useState } from 'react';
import { Alert } from 'react-native';

import { useStores } from '../hooks/useStores';
import { beginPs3Playback, getPs3Generation, recordPs3Diagnostic } from '../utils/PS3Diagnostics';
import { msToTicks } from '../utils/Time';

const VideoPlayer = () => {
	const { rootStore, mediaStore, settingStore } = useStores();

	const player = useRef(null);
	const lastSampleAt = useRef(0);
	const lastBufferState = useRef(null);
	const lastError = useRef('');
	const openedAt = useRef(0);
	// Local player fullscreen state
	const [ isPresenting, setIsPresenting ] = useState(false);
	const [ isDismissing, setIsDismissing ] = useState(false);

	// Set the audio mode when the video player is created
	useEffect(() => {
		Audio.setAudioModeAsync({
			interruptionModeAndroid: InterruptionModeAndroid.DoNotMix,
			interruptionModeIOS: InterruptionModeIOS.DoNotMix,
			playsInSilentModeIOS: true
		});
	}, []);

	// Update the player when media type or uri changes
	useEffect(() => {
		if (mediaStore.type === MediaType.Video && mediaStore.uri) {
			rootStore.set({ didPlayerCloseManually: true });
			openedAt.current = Date.now();
			lastSampleAt.current = 0;
			lastBufferState.current = null;
			lastError.current = '';
			if (settingStore.isPs3DiagnosticEnabled) beginPs3Playback(mediaStore.uri);
			player.current?.loadAsync({
				uri: mediaStore.uri
			}, {
				positionMillis: mediaStore.getPositionMillis(),
				shouldPlay: true
			}).catch(error => {
				console.error('[VideoPlayer] loadAsync failed', error);
				if (settingStore.isPs3DiagnosticEnabled) {
					recordPs3Diagnostic('player_load_error', {
						generation: getPs3Generation(), reason: 'native_load_rejected'
					});
				}
			});
		}
	}, [ mediaStore.type, mediaStore.uri ]);

	// Update the play/pause state when the store indicates it should
	useEffect(() => {
		if (mediaStore.type === MediaType.Video && mediaStore.shouldPlayPause) {
			if (mediaStore.isPlaying) {
				player.current?.pauseAsync();
			} else {
				player.current?.playAsync();
			}
			mediaStore.set({ shouldPlayPause: false });
		}
	}, [ mediaStore.shouldPlayPause ]);

	// Close the player when the store indicates it should stop playback
	useEffect(() => {
		if (mediaStore.type === MediaType.Video && mediaStore.shouldStop) {
			rootStore.set({ didPlayerCloseManually: false });
			closeFullscreen();
			mediaStore.set({ shouldStop: false });
		}
	}, [ mediaStore.shouldStop ]);

	const openFullscreen = () => {
		if (!isPresenting) {
			player.current?.presentFullscreenPlayer()
				.catch(e => {
					console.error(e);
					Alert.alert(e);
				});
		}
	};

	const closeFullscreen = () => {
		if (!isDismissing) {
			player.current?.dismissFullscreenPlayer()
				.catch(e => {
					console.debug(e);
				});
		}
	};

	return (
		<Video
			ref={player}
			usePoster
			posterSource={{ uri: mediaStore.backdropUri }}
			resizeMode='contain'
			useNativeControls
			onReadyForDisplay={() => {
				if (settingStore.isPs3DiagnosticEnabled) {
					recordPs3Diagnostic('video_ready_for_display', {
						generation: getPs3Generation(),
						elapsedMs: Date.now() - openedAt.current
					});
				}
				openFullscreen();
			}}
			onPlaybackStatusUpdate={status => {
				if (!status || !status.isLoaded) {
					const error = status?.error || 'not_loaded';
					if (settingStore.isPs3DiagnosticEnabled && error !== lastError.current) {
						lastError.current = error;
						recordPs3Diagnostic('player_status_error', {
							generation: getPs3Generation(), reason: 'native_unloaded'
						});
					}
					return;
				}
				const { isPlaying, positionMillis, didJustFinish, isBuffering, playableDurationMillis, durationMillis } = status;
				if (settingStore.isPs3DiagnosticEnabled) {
					const now = Date.now();
					if (lastBufferState.current !== isBuffering) {
						lastBufferState.current = isBuffering;
						recordPs3Diagnostic(isBuffering ? 'buffering_started' : 'buffering_ended', {
							generation: getPs3Generation(), positionMs: positionMillis,
							elapsedMs: now - openedAt.current
						});
					}
					if (now - lastSampleAt.current >= 5000) {
						lastSampleAt.current = now;
						recordPs3Diagnostic('player_sample', {
							generation: getPs3Generation(), playing: Boolean(isPlaying),
							buffering: Boolean(isBuffering), positionMs: positionMillis,
							playableMs: playableDurationMillis, durationMs: durationMillis
						});
					}
				}
				if (didJustFinish) {
					rootStore.set({ didPlayerCloseManually: false });
					closeFullscreen();
					return;
				}
				mediaStore.set({
					isPlaying: isPlaying,
					positionTicks: msToTicks(Number.isFinite(positionMillis) ? positionMillis : 0)
				});
			}}
			onFullscreenUpdate={({ fullscreenUpdate }) => {
				switch (fullscreenUpdate) {
					case VideoFullscreenUpdate.PLAYER_WILL_PRESENT:
						setIsPresenting(true);
						rootStore.set({ isFullscreen: true });
						break;
					case VideoFullscreenUpdate.PLAYER_DID_PRESENT:
						setIsPresenting(false);
						break;
					case VideoFullscreenUpdate.PLAYER_WILL_DISMISS:
						setIsDismissing(true);
						break;
					case VideoFullscreenUpdate.PLAYER_DID_DISMISS:
						setIsDismissing(false);
						rootStore.set({ isFullscreen: false });
						mediaStore.reset();
						player.current?.unloadAsync()
							.catch(console.debug);
						break;
				}
			}}
			onError={e => {
				console.error(e);
				if (settingStore.isPs3DiagnosticEnabled) {
					recordPs3Diagnostic('player_error', {
						generation: getPs3Generation(), reason: 'native_video_error'
					});
				}
				Alert.alert(e);
			}}
		/>
	);
};

export default VideoPlayer;
