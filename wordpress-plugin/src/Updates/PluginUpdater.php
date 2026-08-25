<?php
namespace KosmosBridge\Updates;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

/**
 * Supplies WordPress with trusted update metadata from the Kosmos endpoint.
 */
class PluginUpdater {
	const PLUGIN_SLUG      = 'kosmos-bridge';
	const DEFAULT_HOMEPAGE = 'https://kosmos-hub.31-70-92-95.sslip.io';
	const METADATA_URL     = 'https://plugins.kosmos-medien.de/kosmos-bridge/metadata.json';

	/**
	 * @return void
	 */
	public static function boot() {
		$instance = new self();

		add_filter( 'pre_set_site_transient_update_plugins', array( $instance, 'inject_update' ) );
		add_filter( 'plugins_api', array( $instance, 'get_plugin_information' ), 20, 3 );
	}

	/**
	 * @param mixed $transient Plugin update transient.
	 * @return mixed
	 */
	public function inject_update( $transient ) {
		if ( ! is_object( $transient ) || empty( $transient->checked ) || ! is_array( $transient->checked ) ) {
			return $transient;
		}

		$plugin_file = plugin_basename( KOSMOS_BRIDGE_PLUGIN_FILE );

		if ( ! array_key_exists( $plugin_file, $transient->checked ) ) {
			return $transient;
		}

		$metadata = $this->get_metadata();

		if ( empty( $metadata ) || version_compare( (string) $metadata['version'], Options::get_bridge_version(), '<=' ) ) {
			return $transient;
		}

		if ( ! isset( $transient->response ) || ! is_array( $transient->response ) ) {
			$transient->response = array();
		}

		$transient->response[ $plugin_file ] = (object) array(
			'id'           => self::METADATA_URL,
			'slug'         => self::PLUGIN_SLUG,
			'plugin'       => $plugin_file,
			'new_version'  => (string) $metadata['version'],
			'url'          => (string) $metadata['homepage'],
			'package'      => (string) $metadata['download_url'],
			'tested'       => (string) $metadata['tested'],
			'requires'     => (string) $metadata['requires'],
			'requires_php' => (string) $metadata['requires_php'],
		);

		return $transient;
	}

	/**
	 * @param mixed  $result Existing WordPress API result.
	 * @param string $action Requested API action.
	 * @param mixed  $args   Requested API arguments.
	 * @return mixed
	 */
	public function get_plugin_information( $result, $action, $args ) {
		if ( 'plugin_information' !== (string) $action || ! is_object( $args ) || self::PLUGIN_SLUG !== (string) ( $args->slug ?? '' ) ) {
			return $result;
		}

		$metadata = $this->get_metadata();

		if ( empty( $metadata ) ) {
			return $result;
		}

		return (object) array(
			'name'          => (string) $metadata['name'],
			'slug'          => self::PLUGIN_SLUG,
			'version'       => (string) $metadata['version'],
			'author'        => 'Kosmos Medien',
			'homepage'      => (string) $metadata['homepage'],
			'requires'      => (string) $metadata['requires'],
			'tested'        => (string) $metadata['tested'],
			'requires_php'  => (string) $metadata['requires_php'],
			'last_updated'  => (string) $metadata['last_updated'],
			'download_link' => (string) $metadata['download_url'],
			// WordPress 6.2 writes sanitized sections back by array key.
			// Keeping this as an array avoids a fatal error in its details dialog.
			'sections'      => $metadata['sections'],
		);
	}

	/**
	 * @return array<string, mixed>|null
	 */
	private function get_metadata() {
		$response = wp_remote_get(
			self::METADATA_URL,
			array(
				'timeout'     => 10,
				'redirection' => 3,
				'headers'     => array(
					'Accept' => 'application/json',
				),
			)
		);

		if ( is_wp_error( $response ) || 200 !== (int) wp_remote_retrieve_response_code( $response ) ) {
			return null;
		}

		$metadata = json_decode( wp_remote_retrieve_body( $response ), true );
		$metadata = is_array( $metadata ) ? $metadata : array();

		return $this->sanitize_metadata( $metadata );
	}

	/**
	 * @param array<string, mixed> $metadata Untrusted response data.
	 * @return array<string, mixed>|null
	 */
	private function sanitize_metadata( array $metadata ) {
		$version      = isset( $metadata['version'] ) ? trim( (string) $metadata['version'] ) : '';
		$download_url = isset( $metadata['download_url'] ) ? esc_url_raw( (string) $metadata['download_url'] ) : '';
		$homepage     = isset( $metadata['homepage'] ) ? esc_url_raw( (string) $metadata['homepage'] ) : self::DEFAULT_HOMEPAGE;

		if ( ! preg_match( '/^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$/', $version ) ) {
			return null;
		}

		if ( 0 !== strpos( $download_url, 'https://plugins.kosmos-medien.de/kosmos-bridge/' ) || 0 !== strpos( $homepage, 'https://' ) ) {
			return null;
		}

		$sections = isset( $metadata['sections'] ) && is_array( $metadata['sections'] ) ? $metadata['sections'] : array();

		return array(
			'name'         => sanitize_text_field( $metadata['name'] ?? 'Kosmos Bridge' ),
			'version'      => $version,
			'download_url' => $download_url,
			'homepage'     => $homepage,
			'requires'     => sanitize_text_field( $metadata['requires'] ?? '6.9' ),
			'tested'       => sanitize_text_field( $metadata['tested'] ?? '' ),
			'requires_php' => sanitize_text_field( $metadata['requires_php'] ?? '7.4' ),
			'last_updated' => sanitize_text_field( $metadata['last_updated'] ?? '' ),
			'sections'     => array(
				'description' => wp_kses_post( $sections['description'] ?? '' ),
				'changelog'   => wp_kses_post( $sections['changelog'] ?? '' ),
			),
		);
	}
}
