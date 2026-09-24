<?php
namespace KosmosBridge\Registration;

defined( 'ABSPATH' ) || exit;

class RegistrationState {
	const OPTION = 'kosmos_bridge_registration_state';

	public static function current( $identity = null ) {
		$identity = null === $identity ? SecretStore::get_identity() : $identity;
		if ( empty( $identity ) ) {
			return array();
		}
		try {
			$state = OptionStore::read( self::OPTION );
			return self::belongs_to( $state, $identity ) ? $state : array();
		} catch ( \RuntimeException $error ) {
			return array();
		}
	}

	public static function is_registered( $identity = null ) {
		$state = self::current( $identity );
		return isset( $state['status'], $state['last_success_at'] ) && 'ok' === $state['status'] && '' !== $state['last_success_at'];
	}

	public static function begin( array $identity ) {
		try {
			$previous = OptionStore::read( self::OPTION );
			$state = self::belongs_to( $previous, $identity ) ? $previous : array();
			if ( ! empty( $state['attempt_token'] ) && ( ! isset( $state['attempt_expires'] ) || (int) $state['attempt_expires'] >= time() ) ) {
				return false;
			}
			$next = array( 'identity' => SecretStore::fingerprint( $identity ), 'status' => 'pending',
				'message' => 'Registration pending.', 'last_attempt_at' => gmdate( 'c' ),
				'last_success_at' => isset( $state['last_success_at'] ) ? $state['last_success_at'] : '',
				'request_id' => '', 'attempt_token' => wp_generate_uuid4(), 'attempt_expires' => time() + 60 );
			return SecretStore::matches( $identity ) && OptionStore::replace( self::OPTION, $previous, $next ) ? $next : false;
		} catch ( \RuntimeException $error ) {
			return false;
		}
	}

	public static function finish( array $identity, array $attempt, $status, $message, $request_id = '' ) {
		if ( ! SecretStore::matches( $identity ) || ! self::belongs_to( $attempt, $identity ) ) {
			return false;
		}
		$next = $attempt;
		$next['status'] = (string) $status;
		$next['message'] = (string) $message;
		$next['request_id'] = (string) $request_id;
		$next['attempt_token'] = '';
		$next['attempt_expires'] = 0;
		if ( 'ok' === $status ) {
			$next['last_success_at'] = gmdate( 'c' );
		}
		// Late replies cannot overwrite a newer attempt, even for the same identity.
		return OptionStore::replace( self::OPTION, $attempt, $next );
	}

	private static function belongs_to( $state, array $identity ) {
		return is_array( $state ) && isset( $state['identity'] ) && is_string( $state['identity'] )
			&& hash_equals( SecretStore::fingerprint( $identity ), $state['identity'] );
	}
}
