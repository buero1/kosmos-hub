<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Http\RegistrationClient;

defined( 'ABSPATH' ) || exit;

class Registrar {
	/**
	 * @var PayloadFactory
	 */
	private $payload_factory;

	/**
	 * @var RegistrationClient
	 */
	private $client;

	public function __construct() {
		$this->payload_factory = new PayloadFactory();
		$this->client          = new RegistrationClient();
	}

	/**
	 * @param bool $heartbeat Whether this is a heartbeat.
	 * @return bool
	 */
	public function register( $heartbeat = false, $allow_domain_retry = true ) {
		if ( ! SecretStore::ensure_identity() ) {
			return false;
		}
		$identity = SecretStore::get_identity();
		if ( empty( $identity ) ) {
			return false;
		}
		$heartbeat = $heartbeat && RegistrationState::is_registered( $identity );
		$attempt = RegistrationState::begin( $identity );
		if ( false === $attempt ) {
			return false;
		}
		$payload = $this->payload_factory->make( $heartbeat, $identity );
		$response = $this->client->post( $payload, $identity );
		if ( $allow_domain_retry && is_wp_error( $response ) && 'bridge_domain_changed' === $response->get_error_code() ) {
			if ( ! SecretStore::ensure_identity( $payload['site_uuid'] ) ) {
				return false;
			}
			return $this->register( false, false );
		}

		if ( is_wp_error( $response ) ) {
			RegistrationState::finish( $identity, $attempt, 'error', $response->get_error_message() );
			return false;
		}

		return RegistrationState::finish(
			$identity,
			$attempt,
			'ok',
			isset( $response['message'] ) ? (string) $response['message'] : 'Registration accepted.',
			isset( $response['request_id'] ) ? (string) $response['request_id'] : ''
		);
	}
}
