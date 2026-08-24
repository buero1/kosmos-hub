<?php
namespace KosmosBridge\Registration;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class SecretStore {
	/**
	 * @return void
	 */
	public static function ensure_identity() {
		if ( '' === Options::get_site_uuid() ) {
			update_option( Options::SITE_UUID, wp_generate_uuid4(), false );
		}

		if ( '' === Options::get_site_secret() ) {
			update_option( Options::SITE_SECRET, self::generate_secret(), false );
		}
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

