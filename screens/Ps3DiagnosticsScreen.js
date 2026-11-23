/**
 * User-controlled, offline PS3 parity evidence viewer for iPad.
 * The share sheet exports only bounded, redacted route and timing metadata.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Button, ScrollView, Share, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import Colors from '../constants/Colors';
import { Screens } from '../constants/Screens';
import { useStores } from '../hooks/useStores';
import {
	clearPs3Diagnostics, exportPs3Diagnostics, getPs3DiagnosticSnapshot,
	loadPs3Diagnostics, persistPs3Diagnostics, PS3_REFERENCE,
	subscribePs3Diagnostics
} from '../utils/PS3Diagnostics';

const Ps3DiagnosticsScreen = () => {
	const { settingStore } = useStores();
	const [ events, setEvents ] = useState(getPs3DiagnosticSnapshot());
	const [ busy, setBusy ] = useState(false);
	const refresh = useCallback(() => setEvents(getPs3DiagnosticSnapshot()), []);

	useEffect(() => {
		loadPs3Diagnostics().then(refresh).catch(error => {
			console.error('[PS3 diagnostics] Unable to load archive', error);
		});
		return subscribePs3Diagnostics(refresh);
	}, [ refresh ]);

	const exportLog = async () => {
		setBusy(true);
		try {
			await persistPs3Diagnostics();
			await Share.share({ message: exportPs3Diagnostics(), title: 'Jellyfin PS3 parity evidence' });
		} catch (error) {
			console.error('[PS3 diagnostics] Export failed', error);
			Alert.alert('Export failed', String(error));
		} finally {
			setBusy(false);
		}
	};

	const clearLog = () => Alert.alert(
		'Clear PS3 diagnostic evidence?',
		'This removes the local iPad diagnostic history, not any server or PS3 log.',
		[
			{ text: 'Cancel', style: 'cancel' },
			{
				text: 'Clear',
				style: 'destructive',
				onPress: async () => {
					try {
						await clearPs3Diagnostics();
						refresh();
					} catch (error) {
						console.error('[PS3 diagnostics] Clear failed', error);
					}
				}
			}
		]
	);

	return (
		<SafeAreaView style={styles.page} edges={[ 'left', 'right' ]}>
			<ScrollView contentContainerStyle={styles.content}>
				<Text style={styles.title}>PS3 playback parity diagnostics</Text>
				<Text style={styles.line}>PS3 reference: {PS3_REFERENCE}</Text>
				<Text style={styles.line}>Mode: {settingStore.isPs3DiagnosticEnabled ? 'Enabled' : 'Disabled'}</Text>
				<Text style={styles.line}>Captured events: {events.length}</Text>
				<Text style={styles.note}>The iPad can test Jellyfin HTTP MPEG-TS routing, playback startup, buffering and reported ready-for-display events. It cannot reproduce PS3 RSX flips, CellAudio DMA or PS3 memory pressure. Source epochs in iPad logs are observational, not proof that the server changed its transport source.</Text>
				<View style={styles.buttons}>
					<Button title='Refresh' onPress={refresh} />
					<Button title='Export redacted JSON' onPress={exportLog} disabled={busy} />
					<Button title='Clear local log' onPress={clearLog} disabled={busy} color='#b23b38' />
				</View>
				<Text style={styles.heading}>Most recent 50 events</Text>
				{events.slice(-50).reverse().map(item => (
					<Text key={item.seq + ':' + item.utc} style={styles.event}>
						{JSON.stringify(item)}
					</Text>
				))}
			</ScrollView>
		</SafeAreaView>
	);
};
const styles = StyleSheet.create({
	page: { flex: 1, backgroundColor: Colors.blackish },
	content: { padding: 16, paddingBottom: 48 },
	title: { color: Colors.white, fontSize: 20, fontWeight: '600', marginBottom: 12 },
	line: { color: Colors.white, marginBottom: 7 },
	note: { color: Colors.grey1, marginTop: 10, marginBottom: 18, lineHeight: 22 },
	buttons: { marginBottom: 16 },
	heading: { color: Colors.white, fontSize: 17, fontWeight: '600', marginBottom: 10 },
	event: { color: Colors.grey1, fontSize: 11, marginBottom: 8, fontFamily: 'Courier' }
});
Ps3DiagnosticsScreen.displayName = Screens.Ps3DiagnosticsScreen;
export default Ps3DiagnosticsScreen;
