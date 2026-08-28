<?php
namespace KosmosBridge\Http;

use KosmosBridge\Options;
use WP_Error;

defined( 'ABSPATH' ) || exit;

class HubPackageClient {
	const MAX_PACKAGE_BYTES = 20971520;

	/**
	 * Download one package only from the authenticated Kosmos Hub. The Hub also
	 * checks that this site has an active installation run for the package.
	 *
	 * @param int $package_id Checked Hub package identifier.
	 * @return string|WP_Error
	 */
	public static function download( $package_id ) {
		if ( ! is_int( $package_id ) || $package_id < 1 ) {
			return new WP_Error( 'kosmos_bridge_invalid_package_id', 'A valid Hub package identifier is required.', array( 'status' => 400 ) );
		}

		$base_url = Options::get_server_base_url();
		if ( '' === $base_url || ! self::is_allowed_hub_url( $base_url ) ) {
			return new WP_Error( 'kosmos_bridge_invalid_hub_url', 'The configured Kosmos Hub URL is not allowed for package downloads.', array( 'status' => 500 ) );
		}

		$timestamp = gmdate( 'c' );
		$nonce     = self::generate_nonce();
		$body_hash = hash( 'sha256', '' );
		$signature = hash_hmac(
			'sha256',
			implode( '.', array( Options::get_site_uuid(), $timestamp, $nonce, $body_hash ) ),
			Options::get_site_secret()
		);
		$response  = wp_remote_get(
			trailingslashit( $base_url ) . 'api/v1/plugin-packages/' . $package_id . '/download',
			array(
				'timeout'     => 180,
				'redirection' => 0,
				'sslverify'   => true,
				'headers'     => array(
					'Accept'               => 'application/zip',
					'X-Kosmos-Site-UUID'   => Options::get_site_uuid(),
					'X-Kosmos-Timestamp'   => $timestamp,
					'X-Kosmos-Nonce'       => $nonce,
					'X-Kosmos-Body-SHA256' => $body_hash,
					'X-Kosmos-Signature'   => $signature,
				),
			)
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}
		if ( wp_remote_retrieve_response_code( $response ) < 200 || wp_remote_retrieve_response_code( $response ) >= 300 ) {
			return new WP_Error( 'kosmos_bridge_package_download_failed', 'The Hub did not authorize the checked plugin package download.', array( 'status' => 502 ) );
		}
		$package = wp_remote_retrieve_body( $response );
		if ( ! is_string( $package ) || '' === $package || strlen( $package ) > self::MAX_PACKAGE_BYTES ) {
			return new WP_Error( 'kosmos_bridge_invalid_package_download', 'The Hub package download is empty or exceeds the supported package size.', array( 'status' => 502 ) );
		}
		return $package;
	}

	/**
	 * @param string $url Hub base URL.
	 * @return bool
	 */
	private static function is_allowed_hub_url( $url ) {
		$parts = wp_parse_url( $url );
		if ( ! is_array( $parts ) || empty( $parts['host'] ) ) {
			return false;
		}
		if ( isset( $parts['scheme'] ) && 'https' === strtolower( $parts['scheme'] ) ) {
			return true;
		}
		return in_array( strtolower( $parts['host'] ), array( 'localhost', '127.0.0.1' ), true );
	}

	/**
	 * @return string
	 */
	private static function generate_nonce() {
		try {
			return bin2hex( random_bytes( 24 ) );
		} catch ( \Exception $exception ) {
			return wp_generate_password( 48, false, false );
		}
	}
}
