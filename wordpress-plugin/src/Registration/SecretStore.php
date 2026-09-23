<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class SecretStore {
	const IDENTITY_LOCK = 'kosmos_bridge_identity_lock';

	public static function current_domain() {
		$host = strtolower( rtrim( (string) wp_parse_url( home_url( '/' ), PHP_URL_HOST ), '.' ) );
		return 0 === strpos( $host, 'www.' ) ? substr( $host, 4 ) : $host;
	}

	/**
	 * Store the pair atomically; never use the HTTP Host header as an identity.
	 * $replace_uuid is a compare-and-swap guard for a Hub-confirmed legacy conflict.
	 * @return bool Whether this request can safely use the current identity.
	 */
	public static function ensure_identity( $replace_uuid = '' ) {
		$domain = self::current_domain();
		if ( '' === $domain ) {
			return false;
		}
		$identity = get_option( Options::IDENTITY, array() );
		if ( '' === $replace_uuid && self::is_bound( $identity, $domain ) ) {
			return true;
		}
		$lock = array( 'token' => wp_generate_uuid4(), 'expires' => time() + 120 );
		if ( ! add_option( self::IDENTITY_LOCK, $lock, '', false ) ) {
			$existing = get_option( self::IDENTITY_LOCK );
			if ( is_array( $existing ) && (int) $existing['expires'] < time() ) {
				// Remove only the expired lease, never a lock acquired by another request.
				global $wpdb;
				$wpdb->query( $wpdb->prepare( "DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s", self::IDENTITY_LOCK, maybe_serialize( $existing ) ) );
				wp_cache_delete( self::IDENTITY_LOCK, 'options' );
			}
			return false;
		}
		try {
			wp_cache_delete( Options::IDENTITY, 'options' );
			$identity = get_option( Options::IDENTITY, array() );
			if ( self::is_bound( $identity, $domain ) && ( '' === $replace_uuid || $identity['uuid'] !== $replace_uuid ) ) {
				return true;
			}
			$legacy = empty( $identity ) && '' === $replace_uuid;
			$uuid = $legacy ? Options::get_site_uuid() : '';
			$secret = $legacy ? Options::get_site_secret() : '';
			if ( '' === $uuid || '' === $secret ) {
				$uuid = wp_generate_uuid4();
				$secret = self::generate_secret();
			}
			$next = array( 'uuid' => $uuid, 'secret' => $secret, 'domain' => $domain );
			if ( ! update_option( Options::IDENTITY, $next, false ) ) {
				return false;
			}
			delete_option( Options::SITE_UUID );
			delete_option( Options::SITE_SECRET );
			delete_option( Options::LAST_SUCCESS_AT );
			delete_option( Options::LAST_REGISTERED_AT );
			delete_option( Options::LAST_REQUEST_ID );
			update_option( Options::REGISTRATION_STATUS, 'pending', false );
			update_option( Options::REGISTRATION_MESSAGE, 'Domain identity ready; registration pending.', false );
			return true;
		} finally {
			if ( get_option( self::IDENTITY_LOCK ) === $lock ) {
				delete_option( self::IDENTITY_LOCK );
			}
		}
	}

	private static function is_bound( $identity, $domain ) {
		return is_array( $identity ) && ! empty( $identity['uuid'] ) && ! empty( $identity['secret'] )
			&& isset( $identity['domain'] ) && $identity['domain'] === $domain;
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
