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
		$payload  = $this->payload_factory->make( $heartbeat );
		$response = $this->client->post( $payload );

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

