<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Http\RegistrationClient;
use KosmosBridge\Options;

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
	public function register( $heartbeat = false ) {
		if ( ! SecretStore::ensure_identity() ) {
			return false;
		}
		// A copied daily cron event must bootstrap too, not send an unknown UUID without its key.
		$heartbeat = $heartbeat && '' !== Options::get_last_success_at();
		$payload  = $this->payload_factory->make( $heartbeat );
		update_option( Options::LAST_REGISTERED_AT, gmdate( 'c' ), false );
		$response = $this->client->post( $payload );
		if ( is_wp_error( $response ) && 'bridge_domain_changed' === $response->get_error_code() ) {
			if ( ! SecretStore::ensure_identity( $payload['site_uuid'] ) ) {
				return false;
			}
			$payload = $this->payload_factory->make( false );
			update_option( Options::LAST_REGISTERED_AT, gmdate( 'c' ), false );
			$response = $this->client->post( $payload );
		}

		if ( is_wp_error( $response ) ) {
			Options::set_registration_result( 'error', $response->get_error_message() );
			return false;
		}

		Options::set_registration_result(
			'ok',
			isset( $response['message'] ) ? (string) $response['message'] : 'Registration accepted.',
			isset( $response['request_id'] ) ? (string) $response['request_id'] : ''
		);

		return true;
	}
}
