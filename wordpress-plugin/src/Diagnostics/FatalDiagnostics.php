<?php
namespace KosmosBridge\Diagnostics;

defined( 'ABSPATH' ) || exit;

/**
 * Keeps a short, sanitized record of fatal PHP errors for Hub diagnostics.
 */
class FatalDiagnostics {
	const OPTION_NAME          = 'kosmos_bridge_recent_fatal_diagnostics';
	const MAX_STORED_ENTRIES   = 12;
	const MAX_DEBUG_LOG_BYTES  = 65536;
	const MAX_DEBUG_LOG_ENTRIES = 12;
	const MAX_MESSAGE_LENGTH   = 1200;

	/**
	 * @return void
	 */
	public static function register_shutdown_handler() {
		register_shutdown_function( array( self::class, 'capture_last_fatal_error' ) );
	}

	/**
	 * Persist only fatal errors. The handler is best effort because PHP may be
	 * unable to write to the database for the underlying failure itself.
	 *
	 * @return void
	 */
	public static function capture_last_fatal_error() {
		$error = error_get_last();
		if ( ! is_array( $error ) || ! self::is_fatal_error_type( isset( $error['type'] ) ? (int) $error['type'] : 0 ) ) {
			return;
		}

		$entry = self::sanitize_entry(
			array(
				'reported_at' => gmdate( 'c' ),
				'source'      => 'bridge-fatal-capture',
				'level'       => self::error_level_label( (int) $error['type'] ),
				'message'     => isset( $error['message'] ) ? (string) $error['message'] : '',
				'file'        => isset( $error['file'] ) ? (string) $error['file'] : '',
				'line'        => isset( $error['line'] ) ? (int) $error['line'] : 0,
			)
		);
		if ( '' === $entry['message'] ) {
			return;
		}

		try {
			$entries = get_option( self::OPTION_NAME, array() );
			$entries = is_array( $entries ) ? $entries : array();
			array_unshift( $entries, $entry );
			update_option( self::OPTION_NAME, array_slice( $entries, 0, self::MAX_STORED_ENTRIES ), false );
		} catch ( \Throwable $exception ) {
			// The fatal error is still handled by WordPress and its hosting logs.
		}
	}

	/**
	 * Return a bounded diagnostic view. The Hub cannot request arbitrary files.
	 *
	 * @return array
	 */
	public static function recent_diagnostics() {
		$captured_entries  = self::captured_entries();
		$debug_log          = self::debug_log_entries();
		$entries            = array_merge( $captured_entries, $debug_log['entries'] );
		$entries            = array_slice( $entries, 0, self::MAX_STORED_ENTRIES + self::MAX_DEBUG_LOG_ENTRIES );
		$source_descriptions = array();
		if ( ! empty( $captured_entries ) ) {
			$source_descriptions[] = 'Bridge fatal capture';
		}
		if ( $debug_log['available'] ) {
			$source_descriptions[] = 'wp-content/debug.log';
		}

		return array(
			'available'           => true,
			'debug_log_available' => $debug_log['available'],
			'entries'             => $entries,
			'message'             => empty( $source_descriptions )
				? 'No Bridge fatal diagnostics or readable WordPress debug log are available.'
				: 'Diagnostic sources checked: ' . implode( ', ', $source_descriptions ) . '.',
		);
	}

	/**
	 * @return array
	 */
	private static function captured_entries() {
		$stored = get_option( self::OPTION_NAME, array() );
		$stored = is_array( $stored ) ? $stored : array();
		$entries = array();
		foreach ( array_slice( $stored, 0, self::MAX_STORED_ENTRIES ) as $entry ) {
			if ( is_array( $entry ) ) {
				$entries[] = self::sanitize_entry( $entry );
			}
		}

		return array_values( array_filter( $entries, static function ( $entry ) {
			return '' !== $entry['message'];
		} ) );
	}

	/**
	 * @return array{available:bool,entries:array}
	 */
	private static function debug_log_entries() {
		$path = trailingslashit( WP_CONTENT_DIR ) . 'debug.log';
		if ( ! file_exists( $path ) || ! is_readable( $path ) ) {
			return array( 'available' => false, 'entries' => array() );
		}

		$contents = self::tail_file( $path, self::MAX_DEBUG_LOG_BYTES );
		if ( '' === $contents ) {
			return array( 'available' => true, 'entries' => array() );
		}

		$entries = array();
		foreach ( preg_split( '/\r\n|\r|\n/', $contents ) as $line ) {
			if ( ! preg_match( '/(?:PHP Fatal error|Uncaught (?:Error|Exception)|critical error)/i', $line ) ) {
				continue;
			}
			$entries[] = self::debug_log_entry( $line );
		}

		return array(
			'available' => true,
			'entries'   => array_slice( array_values( array_filter( $entries, static function ( $entry ) {
				return '' !== $entry['message'];
			} ) ), -self::MAX_DEBUG_LOG_ENTRIES ),
		);
	}

	/**
	 * @param string $line Debug-log line.
	 * @return array
	 */
	private static function debug_log_entry( $line ) {
		$reported_at = '';
		if ( preg_match( '/^\[([^\]]+)\]/', $line, $matches ) ) {
			$reported_at = self::limit_text( $matches[1], 64 );
		}

		$file = '';
		$line_number = 0;
		if ( preg_match( '/ in (.+) on line (\d+)$/', $line, $matches ) ) {
			$file        = self::normalize_file( $matches[1] );
			$line_number = (int) $matches[2];
		}

		return self::sanitize_entry(
			array(
				'reported_at' => $reported_at,
				'source'      => 'wp-debug-log',
				'level'       => 'fatal error',
				'message'     => $line,
				'file'        => $file,
				'line'        => $line_number,
			)
		);
	}

	/**
	 * @param string $path File path.
	 * @param int    $max_bytes Maximum bytes to read.
	 * @return string
	 */
	private static function tail_file( $path, $max_bytes ) {
		$size = @filesize( $path ); // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
		if ( false === $size ) {
			return '';
		}

		$handle = @fopen( $path, 'rb' ); // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
		if ( false === $handle ) {
			return '';
		}

		$offset = max( 0, (int) $size - $max_bytes );
		if ( $offset > 0 ) {
			fseek( $handle, $offset );
		}
		$contents = stream_get_contents( $handle );
		fclose( $handle );
		if ( ! is_string( $contents ) || '' === $contents ) {
			return '';
		}

		if ( $offset > 0 && false !== strpos( $contents, "\n" ) ) {
			$contents = substr( $contents, strpos( $contents, "\n" ) + 1 );
		}

		return $contents;
	}

	/**
	 * @param array $entry Raw diagnostic entry.
	 * @return array
	 */
	private static function sanitize_entry( $entry ) {
		return array(
			'reported_at' => self::limit_text( isset( $entry['reported_at'] ) ? (string) $entry['reported_at'] : '', 64 ),
			'source'      => in_array( isset( $entry['source'] ) ? $entry['source'] : '', array( 'bridge-fatal-capture', 'wp-debug-log' ), true ) ? $entry['source'] : 'bridge-fatal-capture',
			'level'       => self::limit_text( isset( $entry['level'] ) ? (string) $entry['level'] : 'fatal error', 64 ),
			'message'     => self::sanitize_message( isset( $entry['message'] ) ? (string) $entry['message'] : '' ),
			'file'        => self::normalize_file( isset( $entry['file'] ) ? (string) $entry['file'] : '' ),
			'line'        => max( 0, isset( $entry['line'] ) ? (int) $entry['line'] : 0 ),
		);
	}

	/**
	 * @param int $type PHP error type.
	 * @return bool
	 */
	private static function is_fatal_error_type( $type ) {
		return in_array( $type, array( E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR, E_RECOVERABLE_ERROR ), true );
	}

	/**
	 * @param int $type PHP error type.
	 * @return string
	 */
	private static function error_level_label( $type ) {
		$labels = array(
			E_ERROR             => 'fatal error',
			E_PARSE             => 'parse error',
			E_CORE_ERROR        => 'core error',
			E_COMPILE_ERROR     => 'compile error',
			E_USER_ERROR        => 'user error',
			E_RECOVERABLE_ERROR => 'recoverable error',
		);

		return isset( $labels[ $type ] ) ? $labels[ $type ] : 'fatal error';
	}

	/**
	 * @param string $message Error message.
	 * @return string
	 */
	private static function sanitize_message( $message ) {
		$message = preg_replace( '/((?:password|secret|token|api[_-]?key)\s*(?:=|:)\s*)[^\s&]+/i', '$1[redacted]', $message );
		$message = preg_replace( '/(\b(?:authorization|cookie)\s*:\s*)[^\s]+/i', '$1[redacted]', $message );
		$message = preg_replace_callback(
			'/(?:(?:[A-Za-z]:)?[\\\\\/])[^\s\'\"]+?\.php/i',
			static function ( $matches ) {
				return self::normalize_file( $matches[0] );
			},
			$message
		);
		$message = preg_replace( '/[\x00-\x1F\x7F]+/', ' ', (string) $message );
		return self::limit_text( trim( $message ), self::MAX_MESSAGE_LENGTH );
	}

	/**
	 * @param string $file File path.
	 * @return string
	 */
	private static function normalize_file( $file ) {
		$file = wp_normalize_path( $file );
		if ( '' === $file ) {
			return '';
		}

		$roots = array(
			wp_normalize_path( ABSPATH )         => 'ABSPATH/',
			wp_normalize_path( WP_CONTENT_DIR )  => 'WP_CONTENT_DIR/',
			wp_normalize_path( WP_PLUGIN_DIR )   => 'WP_PLUGIN_DIR/',
		);
		foreach ( $roots as $root => $label ) {
			if ( 0 === strpos( $file, $root ) ) {
				return $label . ltrim( substr( $file, strlen( $root ) ), '/' );
			}
		}

		return basename( $file );
	}

	/**
	 * @param string $value Text to limit.
	 * @param int    $limit Character limit.
	 * @return string
	 */
	private static function limit_text( $value, $limit ) {
		if ( function_exists( 'mb_substr' ) ) {
			return mb_substr( $value, 0, $limit );
		}

		return substr( $value, 0, $limit );
	}
}
