<?php
namespace KosmosBridge\Security;

use KosmosBridge\Options;
use WP_Error;

defined( 'ABSPATH' ) || exit;

class SiteAuth {
	const NONCE_TTL = 600;

	/**
	 * @param \WP_REST_Request $request REST request.
	 * @return true|WP_Error
	 */
	public static function authorize_request( $request ) {
		$site_uuid    = (string) $request->get_header( 'x-kosmos-site-uuid' );
		$timestamp    = (string) $request->get_header( 'x-kosmos-timestamp' );
		$nonce        = (string) $request->get_header( 'x-kosmos-nonce' );
		$body_hash    = strtolower( (string) $request->get_header( 'x-kosmos-body-sha256' ) );
		$signature    = strtolower( (string) $request->get_header( 'x-kosmos-signature' ) );
		$raw_body     = (string) $request->get_body();
		$local_uuid   = Options::get_site_uuid();
		$local_secret = Options::get_site_secret();

		if ( '' === $site_uuid || '' === $timestamp || '' === $nonce || '' === $body_hash || '' === $signature ) {
			return self::unauthorized( 'Missing Kosmos HMAC headers.' );
		}

		if ( '' === $local_uuid || '' === $local_secret ) {
			return self::unauthorized( 'Site identity is not initialized.' );
		}

		if ( $site_uuid !== $local_uuid ) {
			return self::unauthorized( 'Site UUID mismatch.' );
		}

		if ( ! self::timestamp_is_fresh( $timestamp ) ) {
			return self::unauthorized( 'Timestamp outside allowed window.' );
		}

		$computed_hash = hash( 'sha256', $raw_body );
		if ( ! hash_equals( $computed_hash, $body_hash ) ) {
			return self::unauthorized( 'Body hash mismatch.' );
		}

		$message            = implode( '.', array( $site_uuid, $timestamp, $nonce, $body_hash ) );
		$expected_signature = hash_hmac( 'sha256', $message, $local_secret );
		if ( ! hash_equals( $expected_signature, $signature ) ) {
			return self::unauthorized( 'Invalid signature.' );
		}

		$nonce_key = 'kosmos_bridge_nonce_' . md5( $site_uuid . '|' . $timestamp . '|' . $nonce );
		if ( get_transient( $nonce_key ) ) {
			return self::unauthorized( 'Replay detected.' );
		}

		set_transient( $nonce_key, 1, self::NONCE_TTL );
		return true;
	}

	/**
	 * @param string $timestamp ISO-8601 timestamp.
	 * @return bool
	 */
	private static function timestamp_is_fresh( $timestamp ) {
		try {
			$time = new \DateTimeImmutable( $timestamp );
		} catch ( \Exception $exception ) {
			return false;
		}

		$now  = new \DateTimeImmutable( 'now', new \DateTimeZone( 'UTC' ) );
		$diff = abs( $now->getTimestamp() - $time->getTimestamp() );
		return $diff <= self::NONCE_TTL;
	}

	/**
	 * @param string $message Error message.
	 * @return WP_Error
	 */
	private static function unauthorized( $message ) {
		return new WP_Error(
			'kosmos_bridge_auth_failed',
			$message,
			array( 'status' => 401 )
		);
	}
}
