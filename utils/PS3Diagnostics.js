/**
 * Bounded and redacted iPad playback evidence, aligned with PS3 Surf 3.6.5.
 *
 * Raw URLs, access tokens, Jellyfin usernames, media titles and source IDs
 * never enter event records. An opt-in local ring survives normal app exits.
 * No network telemetry is emitted by this module.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';

export const PS3_DIAGNOSTIC_SCHEMA = 'JFI-PS3-1';
export const PS3_REFERENCE = 'v3.6.5-surf.1';
const KEY = '@jellyfin/ps3-diagnostics/v1';
const MAX_EVENTS = 320;
const allowedFields = new Set([
	'generation', 'epoch', 'route', 'routeHash', 'httpStatus', 'method',
	'contentType', 'elapsedMs', 'positionMs', 'playableMs', 'durationMs',
	'buffering', 'playing', 'loaded', 'reason', 'code', 'status', 'count'
]);
let generation = 0;
let epoch = 0;
let events = [];
let seq = 0;
let ready = false;
let writeChain = Promise.resolve();
let pendingWrites = 0;
let lastPersisted = 0;
const listeners = new Set();
const session = Date.now().toString(36);

function hash(value) {
	let result = 2166136261;
	const input = String(value || '');
	for (let i = 0; i < input.length; i++) {
		result ^= input.charCodeAt(i);
		result = Math.imul(result, 16777619) >>> 0;
	}
	return result.toString(16).padStart(8, '0');
}

export function classifyPlaybackRoute(url) {
	try {
		const pathname = new URL(String(url || ''), 'https://invalid.local').pathname.toLowerCase();
		const match = (expression) => new RegExp(expression).test(pathname);
		if (match('/api/surf/(all|sports|movies-series)')) return 'surf-control';
		if (match('/live/(all|sports|movies-series)[.]ts')) return 'ps3-continuous-ts';
		if (pathname.includes('/hls/') || pathname.endsWith('.m3u8')) return 'hls';
		if (match('/(playlist[.]ts[.]m3u|livetv/livestreamfiles)')) return 'livetv';
		if (match('/(videos|audio)/') && /[.](ts|mpegts)$/.test(pathname)) return 'http-ts';
		if (match('/(videos|audio)/') && /[.](mp4|m4v)$/.test(pathname)) return 'http-mp4';
		if (match('/(videos|audio)/')) return 'jellyfin-media';
		if (pathname.includes('/playbackinfo')) return 'playback-info';
		return 'other';
	} catch (error) {
		console.warn('[PS3 diagnostics] Unable to classify route', String(error));
		return 'invalid';
	}
}

export function fingerprintPlaybackRoute(url) {
	try {
		const pathname = new URL(String(url || ''), 'https://invalid.local').pathname;
		return hash(pathname);
	} catch {
		return 'invalid';
	}
}

function scrub(input) {
	if (typeof input === 'number') return Number.isFinite(input) ? Math.round(input) : null;
	if (typeof input === 'boolean') return input;
	if (typeof input !== 'string') return null;
	// All data fields are already semantic labels, but reject accidental URLs
	// and credentials even if a future caller passes untrusted text.
	if (/[:][/][/]|(?:token|password|authorization|apikey|api_key)[=:]/i.test(input)) return 'redacted';
	return input.replace(/[^a-zA-Z0-9_. -]/g, '').slice(0, 72);
}

export function recordPs3Diagnostic(type, payload = {}) {
	try {
		const data = {};
		for (const [ key, value ] of Object.entries(payload || {})) {
			if (allowedFields.has(key)) data[key] = scrub(value);
		}
		const record = {
			schema: PS3_DIAGNOSTIC_SCHEMA,
			seq: ++seq,
			utc: new Date().toISOString(),
			session,
			type: String(type).replace(/[^a-z0-9_]/gi, '').slice(0, 44),
			...data
		};
		events.push(record);
		if (events.length > MAX_EVENTS) {
			events = events.slice(-MAX_EVENTS);
		}
		for (const fn of listeners) {
			try {
				fn();
			} catch (error) {
				console.warn('[PS3 diagnostics] Listener error', error);
			}
		}
		pendingWrites++;
		if (pendingWrites >= 8 || /error|failure|stalled/.test(record.type)) {
			persistPs3Diagnostics();
		}
		return record;
	} catch (error) {
		console.error('[PS3 diagnostics] Event capture failed', error);
		return null;
	}
}

export function beginPs3Playback(url) {
	generation = (generation + 1) >>> 0;
	epoch = 0;
	recordPs3Diagnostic('playback_start', {
		generation, epoch,
		route: classifyPlaybackRoute(url),
		routeHash: fingerprintPlaybackRoute(url)
	});
	return generation;
}

export function observePs3Source(sourceId, bootId, previous) {
	const current = hash(String(sourceId || '') + ':' + String(bootId || ''));
	if (!sourceId) return previous || null;
	if (previous && current !== previous) epoch++;
	if (current !== previous) recordPs3Diagnostic('source_observed', { generation, epoch, routeHash: current });
	return current;
}

export function getPs3Generation() { return generation; }

export async function loadPs3Diagnostics() {
	if (ready) return;
	try {
		const raw = await AsyncStorage.getItem(KEY);
		const parsed = raw ? JSON.parse(raw) : null;
		if (Array.isArray(parsed?.events)) {
			// Do not replace records that arrived while storage was loading.
			const prior = parsed.events.filter(item => item && item.schema === PS3_DIAGNOSTIC_SCHEMA).slice(-MAX_EVENTS);
			events = [ ...prior, ...events ].slice(-MAX_EVENTS);
			seq = Math.max(seq, ...events.map(item => Number(item.seq) || 0));
		}
	} catch (error) {
		console.warn('[PS3 diagnostics] Archived log unavailable', error);
	} finally {
		ready = true;
	}
}

export function subscribePs3Diagnostics(listener) {
	listeners.add(listener);
	return () => listeners.delete(listener);
}

export function getPs3DiagnosticSnapshot() {
	return events.slice();
}

export function persistPs3Diagnostics() {
	pendingWrites = 0;
	const encoded = JSON.stringify({ schema: PS3_DIAGNOSTIC_SCHEMA, reference: PS3_REFERENCE, events });
	writeChain = writeChain.catch(() => null).then(async () => {
		try {
			await AsyncStorage.setItem(KEY, encoded);
			lastPersisted = Date.now();
			return true;
		} catch (error) {
			console.warn('[PS3 diagnostics] Persist failed', error);
			return false;
		}
	});
	return writeChain;
}

export async function clearPs3Diagnostics() {
	events = [];
	seq = 0;
	pendingWrites = 0;
	await persistPs3Diagnostics();
	for (const fn of listeners) {
		try {
			fn();
		} catch (error) {
			console.warn('[PS3 diagnostics] Listener error', error);
		}
	}
}

export function exportPs3Diagnostics() {
	return JSON.stringify({
		schema: PS3_DIAGNOSTIC_SCHEMA,
		reference: PS3_REFERENCE,
		engine: 'iPad AVPlayer via expo-av, not PS3 CellAudio/RSX',
		limitations: [ 'no native PS3 decoder', 'no RSX flip sequence', 'no CellAudio DMA',
			'no true PS3 memory measurement', 'no server-proven source epoch without server status' ],
		generation, epoch, lastPersisted,
		eventCount: events.length, events
	}, null, 2);
}

export const PS3_DIAGNOSTIC_LIMIT = MAX_EVENTS;
