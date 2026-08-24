<?php
namespace KosmosBridge\Http;

use KosmosBridge\Options;
use WP_Error;

defined( 'ABSPATH' ) || exit;

class RegistrationClient {
	/**
	 * @param array $payload Registration payload.
	 * @return array|WP_Error
	 */
	public function post( array $payload ) {
		$base_url = Options::get_server_base_url();

		if ( '' === $base_url ) {
			return new WP_Error(
				'kosmos_bridge_missing_hub_url',
				__( 'KOSMOS_HUB_BASE_URL is not configured.', 'kosmos-bridge' )
			);
		}

		if ( ! $this->is_allowed_url( $base_url ) ) {
			return new WP_Error(
				'kosmos_bridge_insecure_hub_url',
				__( 'Hub URL must use HTTPS unless it points to localhost or a private development address.', 'kosmos-bridge' )
			);
		}

		$body       = wp_json_encode( $payload );
		$request_id = wp_generate_uuid4();
		$timestamp  = gmdate( 'c' );
		$nonce      = self::generate_nonce();
		$body_hash  = hash( 'sha256', $body );
		$signature  = hash_hmac(
			'sha256',
			implode(
				'.',
				array(
					Options::get_site_uuid(),
					$timestamp,
					$nonce,
					$body_hash,
				)
			),
			Options::get_site_secret()
		);

		$response = wp_remote_post(
			trailingslashit( $base_url ) . 'api/v1/registrations',
			array(
				'timeout'     => 15,
				'headers'     => array(
					'Content-Type'         => 'application/json',
					'Accept'               => 'application/json',
					'X-Request-Id'         => $request_id,
					'X-Kosmos-Site-UUID'   => Options::get_site_uuid(),
					'X-Kosmos-Timestamp'   => $timestamp,
					'X-Kosmos-Nonce'       => $nonce,
					'X-Kosmos-Body-SHA256' => $body_hash,
					'X-Kosmos-Signature'   => $signature,
				),
				'body'        => $body,
				'data_format' => 'body',
			)
		);

		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = wp_remote_retrieve_response_code( $response );
		$raw  = wp_remote_retrieve_body( $response );
		$data = json_decode( $raw, true );

		if ( $code < 200 || $code >= 300 ) {
			return new WP_Error(
				'kosmos_bridge_registration_failed',
				is_array( $data ) && isset( $data['detail'] ) ? (string) $data['detail'] : $raw
			);
		}

		return array(
			'request_id' => $request_id,
			'status'     => is_array( $data ) && isset( $data['status'] ) ? (string) $data['status'] : 'ok',
			'message'    => is_array( $data ) && isset( $data['message'] ) ? (string) $data['message'] : 'Registration accepted.',
			'payload'    => is_array( $data ) ? $data : array(),
		);
	}

	/**
	 * @param string $url Target base URL.
	 * @return bool
	 */
	private function is_allowed_url( $url ) {
		$parts = wp_parse_url( $url );
		if ( ! is_array( $parts ) || empty( $parts['host'] ) ) {
			return false;
		}

		if ( isset( $parts['scheme'] ) && 'https' === strtolower( $parts['scheme'] ) ) {
			return true;
		}

		$host = strtolower( $parts['host'] );
		return in_array( $host, array( 'localhost', '127.0.0.1' ), true );
	}

	/**
	 * @return string
	 */
	private static function generate_nonce() {
		try {
			return bin2hex( random_bytes( 16 ) );
		} catch ( \Exception $exception ) {
			return wp_generate_password( 32, false, false );
		}
	}
}
