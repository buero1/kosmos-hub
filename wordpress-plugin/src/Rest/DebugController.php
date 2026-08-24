<?php
namespace KosmosBridge\Rest;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

class DebugController {
	/**
	 * @return void
	 */
	public static function register_routes() {
		register_rest_route(
			'kosmos-bridge/v1',
			'/status',
			array(
				'methods'             => 'GET',
				'permission_callback' => static function () {
					return current_user_can( 'manage_options' );
				},
				'callback'            => static function () {
					return rest_ensure_response(
						array(
							'site_uuid'          => Options::get_site_uuid(),
							'server_base_url'    => Options::get_server_base_url(),
							'registration_status' => Options::get_registration_status(),
							'last_registered_at' => Options::get_last_registered_at(),
							'last_success_at'    => Options::get_last_success_at(),
							'mcp_endpoint'       => Options::get_mcp_endpoint(),
							'abilities_api'      => function_exists( 'wp_register_ability' ),
						)
					);
				},
			)
		);
	}
}
