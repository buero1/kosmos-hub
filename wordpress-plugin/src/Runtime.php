<?php
namespace KosmosBridge;

defined( 'ABSPATH' ) || exit;

class Runtime {

	/** Registration must not mix classes loaded before and after a self-update. */
	public static function is_current() {
		$header = get_file_data( dirname( __DIR__ ) . '/kosmos-bridge.php', array( 'Version' => 'Version' ) );
		return ! empty( $header['Version'] ) && Options::BRIDGE_VERSION === $header['Version'];
	}
}
