import {
	beginPs3Playback, classifyPlaybackRoute, clearPs3Diagnostics,
	exportPs3Diagnostics, fingerprintPlaybackRoute, getPs3DiagnosticSnapshot,
	getPs3Generation, observePs3Source, recordPs3Diagnostic, PS3_DIAGNOSTIC_LIMIT
} from '../PS3Diagnostics';

describe('PS3 diagnostics', () => {
	beforeEach(async () => {
		await clearPs3Diagnostics();
	});

	it('classifies PS3 TS, HLS and Jellyfin stream paths without storing credentials', () => {
		expect(classifyPlaybackRoute('http://server:8103/live/all.ts?api_key=SECRET')).toBe('ps3-continuous-ts');
		expect(classifyPlaybackRoute('http://server:8103/hls/sports/index.m3u8')).toBe('hls');
		expect(classifyPlaybackRoute('http://server:8096/Videos/abcd/stream.ts')).toBe('http-ts');
		expect(classifyPlaybackRoute('http://server/Items/abcd/PlaybackInfo')).toBe('playback-info');
		expect(fingerprintPlaybackRoute('http://a/Videos/x/stream.ts?api_key=X')).toBe(
			fingerprintPlaybackRoute('http://b/Videos/x/stream.ts?api_key=Y')
		);
	});

	it('tracks session and synthetic source epochs without inventing hardware presentation', () => {
		const gen = beginPs3Playback('http://server/live/all.ts');
		expect(getPs3Generation()).toBe(gen);
		let prev = observePs3Source('channel-a', 'boot-a', null);
		prev = observePs3Source('channel-a', 'boot-a', prev);
		prev = observePs3Source('channel-b', 'boot-b', prev);
		expect(prev).toBeTruthy();
		const sourceEvents = getPs3DiagnosticSnapshot().filter(e => e.type === 'source_observed');
		expect(sourceEvents).toHaveLength(2);
		expect(sourceEvents[1].epoch).toBe(1);
	});

	it('bounds history and redacts strings that contain URLs or secrets', () => {
		for (let i = 0; i < PS3_DIAGNOSTIC_LIMIT + 20; i++) {
			recordPs3Diagnostic('test', { reason: 'https://secret/token=ABC', positionMs: i });
		}
		const events = getPs3DiagnosticSnapshot();
		expect(events).toHaveLength(PS3_DIAGNOSTIC_LIMIT);
		expect(events[0].reason).toBe('redacted');
		const archive = exportPs3Diagnostics();
		expect(archive).not.toContain('secret/token');
		expect(archive).toContain('no RSX flip sequence');
	});
});
