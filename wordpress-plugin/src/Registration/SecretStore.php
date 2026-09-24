<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class SecretStore {
	const IDENTITY_LOCK = 'kosmos_bridge_identity_lock';
	const IDENTITY_EVENTS = 'kosmos_bridge_identity_events';

	public static function current_domain() {
		$host = strtolower( rtrim( (string) wp_parse_url( home_url( '/' ), PHP_URL_HOST ), '.' ) );
		return 0 === strpos( $host, 'www.' ) ? substr( $host, 4 ) : $host;
	}

	public static function get_identity() {
		try {
			$identity = OptionStore::read( Options::IDENTITY );
			return self::is_bound( $identity, self::current_domain() ) ? $identity : array();
		} catch ( \RuntimeException $error ) {
			return array();
		}
	}

	public static function fingerprint( array $identity ) {
		return hash( 'sha256', serialize( array( $identity['uuid'], $identity['secret'], $identity['domain'], Options::get_server_base_url() ) ) );
	}

	public static function matches( array $identity ) {
		$current = self::get_identity();
		return ! empty( $current ) && hash_equals( self::fingerprint( $identity ), self::fingerprint( $current ) );
	}

	public static function ensure_identity( $replace_uuid = '' ) {
		$lock = null;
		try {
			$domain = self::current_domain();
			if ( '' === $domain ) {
				return false;
			}
			$identity = OptionStore::read( Options::IDENTITY );
			if ( '' === $replace_uuid && self::is_bound( $identity, $domain ) ) {
				return true;
			}
			$existing = OptionStore::read( self::IDENTITY_LOCK );
			if ( null !== $existing && ( ! is_array( $existing ) || ! isset( $existing['expires'] ) || (int) $existing['expires'] >= time() ) ) {
				return false;
			}
			$lease = array( 'token' => wp_generate_uuid4(), 'expires' => time() + 120 );
			if ( ! OptionStore::replace( self::IDENTITY_LOCK, $existing, $lease ) ) {
				return false;
			}
			$lock = $lease;
			$identity = OptionStore::read( Options::IDENTITY );
			if ( self::is_bound( $identity, $domain ) && ( '' === $replace_uuid || $identity['uuid'] !== $replace_uuid ) ) {
				return true;
			}
			if ( null !== $identity && ! self::is_valid( $identity ) ) {
				return false;
			}
			$legacy_uuid = OptionStore::read( Options::SITE_UUID );
			$legacy_secret = OptionStore::read( Options::SITE_SECRET );
			$legacy = null === $identity && '' === $replace_uuid;
			$uuid = $legacy && is_string( $legacy_uuid ) ? $legacy_uuid : '';
			$secret = $legacy && is_string( $legacy_secret ) ? $legacy_secret : '';
			if ( ( '' === $uuid ) !== ( '' === $secret ) ) {
				return false;
			}
			$reason = $legacy ? ( '' !== $uuid ? 'legacy-migration' : 'fresh-install' ) : ( '' !== $replace_uuid ? 'hub-domain-conflict' : 'domain-change' );
			if ( '' === $uuid || '' === $secret ) {
				$uuid = wp_generate_uuid4();
				$secret = self::generate_secret();
			}
			$next = array( 'uuid' => $uuid, 'secret' => $secret, 'domain' => $domain );
			if ( OptionStore::read( self::IDENTITY_LOCK ) !== $lock ) {
				return false;
			}
			if ( ! OptionStore::replace( Options::IDENTITY, $identity, $next ) ) {
				return false;
			}
			// In-flight pre-0.3.67 requests must not regenerate deleted legacy keys.
			if ( ! $legacy ) {
				OptionStore::remove( Options::SITE_UUID, $legacy_uuid );
				OptionStore::remove( Options::SITE_SECRET, $legacy_secret );
			}
			$events = OptionStore::read( self::IDENTITY_EVENTS );
			$history = is_array( $events ) ? $events : array();
			$history[] = array( 'at' => gmdate( 'c' ), 'reason' => $reason,
				'previous_uuid' => is_array( $identity ) ? $identity['uuid'] : $legacy_uuid,
				'uuid' => $uuid, 'previous_domain' => is_array( $identity ) ? $identity['domain'] : '',
				'domain' => $domain, 'bridge_version' => Options::get_bridge_version() );
			OptionStore::replace( self::IDENTITY_EVENTS, $events, array_slice( $history, -10 ) );
			return true;
		} catch ( \RuntimeException $error ) {
			return false;
		} finally {
			if ( null !== $lock ) {
				OptionStore::remove( self::IDENTITY_LOCK, $lock );
			}
		}
	}

	private static function is_valid( $identity ) {
		return is_array( $identity ) && isset( $identity['uuid'], $identity['secret'], $identity['domain'] )
			&& is_string( $identity['uuid'] ) && '' !== $identity['uuid']
			&& is_string( $identity['secret'] ) && '' !== $identity['secret']
			&& is_string( $identity['domain'] ) && '' !== $identity['domain'];
	}

	private static function is_bound( $identity, $domain ) {
		return self::is_valid( $identity ) && $identity['domain'] === $domain;
	}

	/**
	 * @return string
	 */
	private static function generate_secret() {
		try {
			return bin2hex( random_bytes( 32 ) );
		} catch ( \Exception $exception ) {
			return wp_generate_password( 64, true, true );
		}
	}
}
