/**
 * Copyright (c) 2025 Jellyfin Contributors
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/.
 */

import { AUTHORIZATION_PARAMETER } from '@jellyfin/sdk/lib/constants';
import { MediaType } from '@jellyfin/sdk/lib/generated-client/models/media-type';
import { nativeApplicationVersion } from 'expo-application';
import { activateKeepAwake, deactivateKeepAwake } from 'expo-keep-awake';
import React, { ForwardRefRenderFunction, useImperativeHandle, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Alert, BackHandler, Platform } from 'react-native';
import type { WebView, WebViewMessageEvent } from 'react-native-webview';

import { LEGACY_AUTHORIZATION_PARAMETER } from '../constants/api';
import { useStores } from '../hooks/useStores';
import DownloadModel from '../models/DownloadModel';
import { getAppName, getDeviceProfile, getSafeDeviceName } from '../utils/Device';
import { classifyPlaybackRoute, fingerprintPlaybackRoute, recordPs3Diagnostic } from '../utils/PS3Diagnostics';
import ps3WebHook from '../utils/PS3WebTrace';
import StaticScriptLoader from '../utils/StaticScriptLoader';
import { openBrowser } from '../utils/WebBrowser';

import RefreshWebView, { type RefreshWebViewProps } from './RefreshWebView';

type NativeShellWebViewProps = Omit<RefreshWebViewProps, 'isRefreshing' | 'onRefresh'>;

/**
 * A RefreshWebView with NativeShell script injection and message handling.
 */
const NativeShellWebView: ForwardRefRenderFunction<WebView, NativeShellWebViewProps> = (props, outerRef) => {
	const innerRef = useRef<WebView>(null);
	useImperativeHandle(outerRef, () => innerRef.current as WebView, []);

	const { rootStore, downloadStore, serverStore, mediaStore, settingStore } = useStores();
	const [ isRefreshing, setIsRefreshing ] = useState(false);
	const { t } = useTranslation();

	const server = serverStore.servers[settingStore.activeServer];

	const injectedJavaScript = `
window.ExpoAppInfo = {
	appName: ${JSON.stringify(settingStore.isPs3DiagnosticEnabled ? 'PS3' : getAppName())},
	appVersion: ${JSON.stringify(settingStore.isPs3DiagnosticEnabled ? '0.1' : nativeApplicationVersion)},
	deviceId: ${JSON.stringify(rootStore.deviceId)},
	deviceName: ${JSON.stringify(settingStore.isPs3DiagnosticEnabled ? 'PS3' : getSafeDeviceName())}
};

window.ExpoAppSettings = {
	isNativeVideoPlayerEnabled: ${settingStore.isNativeVideoPlayerEnabled || settingStore.isPs3DiagnosticEnabled},
	isExperimentalNativeAudioPlayerEnabled: ${settingStore.isExperimentalNativeAudioPlayerEnabled}
};

window.ExpoVideoProfile = ${JSON.stringify(getDeviceProfile({ enableFmp4: settingStore.isFmp4Enabled, ps3Diagnostic: settingStore.isPs3DiagnosticEnabled }))};

function postExpoEvent(event, data) {
	window.ReactNativeWebView.postMessage(JSON.stringify({
		event: event,
		data: data
	}));
}

${settingStore.isPs3DiagnosticEnabled ? ps3WebHook : ''}

${StaticScriptLoader.scripts.NativeAudioPlayer}
${StaticScriptLoader.scripts.NativeVideoPlayer}

${StaticScriptLoader.scripts.NativeShell}

${StaticScriptLoader.scripts.ExpoRouterShim}

window.onerror = console.error;

// Fixes background audio playback for server versions <12.0
if ('audioSession' in navigator) {
	navigator.audioSession.type = 'playback';
}

true;
`;

	const onRefresh = () => {
		// Disable pull to refresh when in fullscreen
		if (rootStore.isFullscreen) return;

		// Stop media playback in native players
		mediaStore.set({ shouldStop: true });

		setIsRefreshing(true);
		innerRef.current?.reload();
		setIsRefreshing(false);
	};

	const onMessage = ({ nativeEvent: state }: WebViewMessageEvent) => {
		try {
			const { event, data } = JSON.parse(state.data);
			switch (event) {
				case 'PS3Diagnostic.network':
					if (settingStore.isPs3DiagnosticEnabled) {
						recordPs3Diagnostic('network_response', {
							route: data?.route, httpStatus: data?.httpStatus,
							contentType: data?.contentType, elapsedMs: data?.elapsedMs,
							status: data?.status
						});
					}
					break;
				case 'AppHost.exit':
					BackHandler.exitApp();
					break;
				case 'enableFullscreen':
					rootStore.set({ isFullscreen: true });
					break;
				case 'disableFullscreen':
					rootStore.set({ isFullscreen: false });
					break;
				case 'downloadFile': {
					console.log('Download item', data);

					const title = data?.item?.item?.Name || data?.item?.filename;

					// Get the API key from the download URL
					let apiKey;
					try {
						const url = new URL(data.item.url);
						apiKey = url.searchParams.get(AUTHORIZATION_PARAMETER) || url.searchParams.get(LEGACY_AUTHORIZATION_PARAMETER);
					} catch (e) {
						console.error('[NativeShellWebView] downloadFile: failed to get api key from download url', data.item?.url);
						Alert.alert(
							t('alerts.downloadFailed.title'),
							t('alerts.downloadFailed.description', { title })
						);
						break;
					}

					// Validate that required fields are present
					if (!apiKey || !data.item?.filename || !data.item?.item) {
						console.error('[NativeShellWebView] downloadFile: missing required fields', {
							hasApiKey: !!apiKey,
							hasFilename: !!data?.item?.filename,
							hasItem: !!data?.item?.item
						});
						Alert.alert(
							t('alerts.downloadFailed.title'),
							t('alerts.downloadFailed.description', { title })
						);
						break;
					}

					downloadStore.add(new DownloadModel(
						data.item.item,
						server.urlString,
						apiKey,
						data.item.filename,
						data.item.url
					));
					break;
				}
				case 'openUrl':
					console.log('Opening browser for external url', data.url);
					openBrowser(data.url);
					break;
				case 'updateMediaSession':
					// Keep the screen awake when music is playing
					if (settingStore.isScreenLockEnabled) {
						activateKeepAwake();
					}
					break;
				case 'hideMediaSession':
					// When music session stops disable keep awake
					if (settingStore.isScreenLockEnabled) {
						deactivateKeepAwake();
					}
					break;
				case 'ExpoAudioPlayer.play':
				case 'ExpoVideoPlayer.play':
					if (settingStore.isPs3DiagnosticEnabled && event === 'ExpoVideoPlayer.play') {
						recordPs3Diagnostic('webview_play_request', {
							route: classifyPlaybackRoute(data?.url),
							routeHash: fingerprintPlaybackRoute(data?.url)
						});
					}
					mediaStore.set({
						type: event === 'ExpoAudioPlayer.play' ? MediaType.Audio : MediaType.Video,
						uri: data.url,
						backdropUri: data.backdropUrl,
						isFinished: false,
						positionTicks: data.playerStartPositionTicks
					});
					break;
				case 'ExpoAudioPlayer.playPause':
				case 'ExpoVideoPlayer.playPause':
					mediaStore.set({ shouldPlayPause: true });
					break;
				case 'ExpoAudioPlayer.stop':
				case 'ExpoVideoPlayer.stop':
					mediaStore.set({ shouldStop: true });
					break;
				case 'console.debug':
					// console.debug('[Browser Console]', data);
					break;
				case 'console.error':
					console.error('[Browser Console]', data);
					break;
				case 'console.info':
					// console.info('[Browser Console]', data);
					break;
				case 'console.log':
					// console.log('[Browser Console]', data);
					break;
				case 'console.warn':
					console.warn('[Browser Console]', data);
					break;
				default:
					console.debug('[HomeScreen.onMessage]', event, JSON.stringify(data));
			}
		} catch (ex) {
			console.warn('Exception handling message', state.data);
		}
	};

	return (
		<RefreshWebView
			ref={innerRef}
			// Pass through additional props
			{...props}
			// Allow any origin blocking can break various things like book playback
			originWhitelist={[ '*' ]}
			applicationNameForUserAgent={settingStore.isPs3DiagnosticEnabled ? 'JellyfinPS3/0.1' : undefined}
			source={{ uri: server.urlString }}
			// Inject javascript for NativeShell
			// This method is preferred, but only supported on iOS currently
			injectedJavaScriptBeforeContentLoaded={Platform.OS === 'ios' ? injectedJavaScript : undefined}
			// Fallback for non-iOS
			injectedJavaScript={Platform.OS !== 'ios' ? injectedJavaScript : undefined}
			onMessage={onMessage}
			isRefreshing={isRefreshing}
			onRefresh={onRefresh}
			// Make scrolling feel faster
			decelerationRate='normal'
			// Media playback options to fix video player
			allowsInlineMediaPlayback={true}
			mediaPlaybackRequiresUserAction={false}
			showsVerticalScrollIndicator={false}
			showsHorizontalScrollIndicator={false}
		/>
	);
};

const ForwardRefNativeShellWebview = React.forwardRef(NativeShellWebView);
ForwardRefNativeShellWebview.displayName = 'NativeShellWebView';

export default ForwardRefNativeShellWebview;
