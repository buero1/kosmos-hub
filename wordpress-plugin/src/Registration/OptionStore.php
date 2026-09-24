<?php
namespace KosmosBridge\Registration;

defined( 'ABSPATH' ) || exit;

/** Uncached reads and database compare-and-swap for identity-critical options. */
class OptionStore {
	public static function read( $name ) {
		global $wpdb;
		$row = $wpdb->get_row( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", $name ) );
		if ( ! empty( $wpdb->last_error ) ) {
			throw new \RuntimeException( 'Bridge identity storage is unavailable.' );
		}
		return null === $row ? null : maybe_unserialize( $row->option_value );
	}

	public static function replace( $name, $expected, $value ) {
		global $wpdb;
		$serialized = maybe_serialize( $value );
		if ( null === $expected ) {
			// Unlike add_option(), this never overwrites a concurrently inserted row.
			$sql = $wpdb->prepare( "INSERT IGNORE INTO {$wpdb->options} (option_name, option_value, autoload) VALUES (%s, %s, 'no')", $name, $serialized );
		} else {
			$sql = $wpdb->prepare( "UPDATE {$wpdb->options} SET option_value = %s, autoload = 'no' WHERE option_name = %s AND BINARY option_value = %s", $serialized, $name, maybe_serialize( $expected ) );
		}
		$changed = 1 === $wpdb->query( $sql );
		self::invalidate( $name );
		return $changed;
	}

	public static function remove( $name, $expected ) {
		if ( null === $expected ) {
			return false;
		}
		global $wpdb;
		$changed = 1 === $wpdb->query( $wpdb->prepare( "DELETE FROM {$wpdb->options} WHERE option_name = %s AND BINARY option_value = %s", $name, maybe_serialize( $expected ) ) );
		self::invalidate( $name );
		return $changed;
	}

	private static function invalidate( $name ) {
		wp_cache_delete( $name, 'options' );
		wp_cache_delete( 'notoptions', 'options' );
		wp_cache_delete( 'alloptions', 'options' );
	}
}
